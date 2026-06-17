"""Flight-lookup endpoint — LORA's first action button.

First slice of the integrations-views untangling refactor: moved out of the
monolithic views module unchanged. The only cleanup is folding the four
near-identical timeline writes into _record_flight_timeline(); the stored
payloads and all HTTP responses are byte-for-byte the same as before.
"""

import json
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from apps.claims.models import Claim, ClaimUpdateTimeline
from apps.integrations.services import (
    fetch_zendesk_ticket,
    post_zendesk_comment,
    _compose_flight_details as compose_flight_details,
)
from apps.integrations.flight_lookup import (
    FlightProviderNotConfigured,
    analyze_flight_match,
    derive_flight_verdict,
    find_candidate_flights,
    format_candidates_note,
    format_flight_note,
    format_no_number_note,
    format_not_found_note,
    lookup_flight,
    normalize_flight,
    parse_airline_hint,
    parse_airport_hint,
    parse_date_hint,
    parse_flight_query,
    parse_time_hint,
)
from apps.integrations.views.auth import ZendeskSidebarAuth

logger = logging.getLogger(__name__)

# Timeline update_type value written for a flight-lookup result. Matches a member
# of ClaimUpdateTimeline.UPDATE_TYPE_CHOICES (the model exposes no TYPE_* const).
TIMELINE_TYPE_INFO_UPDATED = 'INFO_UPDATED'

# Beyond this many days in the past, an empty AeroDataBox answer is treated as a
# data-plan history-window gap rather than proof the flight never existed.
FLIGHT_HISTORY_WINDOW_DAYS = 14


def _record_flight_timeline(claim, changes, llm_summary):
    """Write the INFO_UPDATED timeline row for a flight-lookup result. Folded out
    of the four near-identical ClaimUpdateTimeline.objects.create() calls in the
    view; the stored row is identical to before."""
    ClaimUpdateTimeline.objects.create(
        claim=claim,
        zendesk_ticket_id=claim.zd_ticket_id,
        update_type=TIMELINE_TYPE_INFO_UPDATED,
        changes_summary=json.dumps({'flight_lookup': changes}),
        llm_summary=llm_summary,
    )


class ZendeskFlightLookupView(APIView):
    """POST /api/integrations/zd/flight-lookup/
    Body: {ticket_id, refresh?: bool}

    LORA's first action button: looks up the flight on AeroDataBox,
    AI-cross-checks it against the client's report (selected airport, loss
    time/circumstances) and posts an internal note on the ticket. On
    not-found, the candidate rescue lists likely departures from the stated
    airport.

    Claim-first, fields-fallback: a linked claim supplies the flight details
    (and caches the result — the money saver). Without a claim, LORA reads
    the same structured Zendesk ticket fields the claim would have been built
    from (no ticket-text scraping) and runs a fresh, uncached lookup.
    Never touches claim.status. Auth: ZendeskSidebarAuth."""

    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='flight-lookup')
        if auth_error:
            return auth_error

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        if not ticket_id:
            return Response({'error_message': 'No ticket id received.'},
                            status=status.HTTP_200_OK)
        from rest_framework.fields import BooleanField
        refresh = request.data.get('refresh') in BooleanField.TRUE_VALUES
        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()

        if claim:
            flight_details = claim.flight_details
        else:
            # Claimless ticket: read the same structured Zendesk fields the
            # claim would have been built from (never the ticket text).
            ticket_data = fetch_zendesk_ticket(ticket_id)
            if ticket_data is None:
                return Response(
                    {'error_message': "Couldn't read this ticket's fields from Zendesk."},
                    status=status.HTTP_200_OK)
            flight_details = compose_flight_details(ticket_data.get('custom_fields') or [])
            if not flight_details:
                return Response(
                    {'error_message': "This ticket's flight fields (Flight #, "
                                      "Date & Time, Airport) are empty."},
                    status=status.HTTP_200_OK)

        query = parse_flight_query(flight_details)
        if not query:
            return self._handle_no_number(claim, ticket_id, flight_details)

        if claim and claim.flight_data and not refresh:
            return Response({'flight': claim.flight_data, 'analysis': None,
                             'cached': True, 'note_posted': False},
                            status=status.HTTP_200_OK)

        try:
            raw_legs = lookup_flight(query['number'], query['date'])
        except FlightProviderNotConfigured:
            return Response(
                {'error': 'AeroDataBox API key is not configured in System settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if raw_legs is None:
            return Response({'error': 'Flight data provider unavailable. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)

        if not raw_legs:
            return self._handle_not_found(claim, ticket_id, query, flight_details)

        flight = normalize_flight(raw_legs)
        analysis = analyze_flight_match(
            claim, flight,
            flight_details_text='' if claim else flight_details)
        verdict = derive_flight_verdict(True, analysis)
        flight['verdict'] = verdict

        # Persist the flight data and its timeline entry as one unit — a crash
        # between them would leave flight_data saved with no matching timeline
        # row. The external Zendesk note post stays OUTSIDE the transaction (no
        # network I/O while holding it open).
        if claim:
            with transaction.atomic():
                claim.flight_data = flight
                claim.flight_data_updated_at = timezone.now()
                claim.save(update_fields=['flight_data', 'flight_data_updated_at', 'updated_at'])
                _record_flight_timeline(
                    claim,
                    {**query, 'found': True, 'verdict': verdict['level']},
                    analysis.summary if analysis else '',
                )

        note_posted = self._post_note(ticket_id, format_flight_note(flight, analysis, verdict))

        subject = f"claim #{claim.id}" if claim else f"claimless ticket {ticket_id}"
        logger.info("Flight lookup for %s: %s %s found, verdict=%s",
                    subject, query['number'], query['date'], verdict['level'])
        return Response({'flight': flight, 'analysis': self._analysis_dict(analysis),
                         'verdict': verdict, 'cached': False, 'note_posted': note_posted,
                         'claimless': claim is None},
                        status=status.HTTP_200_OK)

    def _handle_no_number(self, claim, ticket_id, flight_details):
        """No flight number on the ticket: search departures by airport +
        date (narrowed to the form's airline when present) and let the AI
        rank the candidates against the client's report."""
        airport = parse_airport_hint(flight_details)
        date = parse_date_hint(flight_details)
        if not airport or not date:
            source = 'claim' if claim else "ticket's flight fields"
            return Response(
                {'error_message': f"Couldn't read a flight number and date from this {source}. "
                                  "Searching without a number needs at least the Airport "
                                  "and Date fields."},
                status=status.HTTP_200_OK)

        airline_code = parse_airline_hint(flight_details) or ''
        try:
            candidates = find_candidate_flights(
                airport, date, parse_time_hint(flight_details),
                airline_code=airline_code)
        except FlightProviderNotConfigured:
            return Response(
                {'error': 'AeroDataBox API key is not configured in System settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if candidates is None:
            return Response({'error': 'Flight data provider unavailable. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)
        if not candidates:
            carrier = f'{airline_code} ' if airline_code else ''
            return Response(
                {'error_message': f"No flight number on this ticket, and no {carrier}"
                                  f"departures found from {airport} on {date}.",
                 'claimless': claim is None},
                status=status.HTTP_200_OK)

        analysis = analyze_flight_match(
            claim, None, candidates,
            flight_details_text='' if claim else flight_details)
        verdict = derive_flight_verdict(False, analysis, has_candidates=True)
        note = format_no_number_note(date, airport, candidates, analysis,
                                     verdict, airline_code)
        note_posted = self._post_note(ticket_id, note)
        if claim:
            _record_flight_timeline(
                claim,
                {'number': None, 'date': date, 'airport': airport,
                 'found': False, 'candidates': len(candidates)},
                analysis.summary if analysis else '',
            )
        return Response({'error_message': 'No flight number on this ticket.',
                         'candidates': candidates,
                         'analysis': self._analysis_dict(analysis),
                         'verdict': verdict, 'claimless': claim is None,
                         'note_posted': note_posted}, status=status.HTTP_200_OK)

    def _handle_not_found(self, claim, ticket_id, query, flight_details):
        """Candidate rescue: when the flight number is not found, list likely
        departures from the client's stated airport so agents get leads
        instead of a dead end. Works with or without a claim — the hints come
        from the flight details either way."""
        from datetime import date as date_cls, timedelta

        error_message = f"No flight found for {query['number']} on {query['date']}."
        try:
            # Empty answers for old dates are usually the data plan's history
            # window, not proof the flight never existed (verified live:
            # Basic serves ~3 weeks back; beyond that comes back empty).
            if date_cls.fromisoformat(query['date']) < timezone.localdate() - timedelta(days=FLIGHT_HISTORY_WINDOW_DAYS):
                error_message += (" Note: this date may be beyond the AeroDataBox plan's "
                                  "history window — older flights need a higher plan.")
        except ValueError:
            pass
        airport = parse_airport_hint(flight_details)
        candidates = None
        if airport:
            try:
                candidates = find_candidate_flights(
                    airport, query['date'], parse_time_hint(flight_details))
            except FlightProviderNotConfigured:
                candidates = None

        if candidates:
            analysis = analyze_flight_match(
                claim, None, candidates,
                flight_details_text='' if claim else flight_details)
            verdict = derive_flight_verdict(False, analysis, has_candidates=True)
            note = format_candidates_note(
                query['number'], query['date'], airport, candidates, analysis, verdict)
            note_posted = self._post_note(ticket_id, note)
            if claim:
                _record_flight_timeline(
                    claim,
                    {**query, 'found': False, 'candidates': len(candidates)},
                    analysis.summary if analysis else '',
                )
            return Response({'error_message': error_message, 'candidates': candidates,
                             'analysis': self._analysis_dict(analysis),
                             'verdict': verdict, 'claimless': claim is None,
                             'note_posted': note_posted}, status=status.HTTP_200_OK)

        verdict = derive_flight_verdict(False, None)
        note_posted = self._post_note(
            ticket_id, format_not_found_note(query['number'], query['date'], verdict))
        if claim:
            _record_flight_timeline(
                claim,
                {**query, 'found': False, 'candidates': 0},
                '',
            )
        return Response({'error_message': error_message, 'verdict': verdict,
                         'claimless': claim is None, 'note_posted': note_posted},
                        status=status.HTTP_200_OK)

    @staticmethod
    def _post_note(ticket_id, body):
        """Post an internal note; never let a Zendesk hiccup fail the lookup."""
        try:
            return bool(post_zendesk_comment(ticket_id, body, is_internal=True))
        except Exception as e:
            logger.warning("Flight note post failed for ticket %s: %s", ticket_id, e)
            return False

    @staticmethod
    def _analysis_dict(analysis):
        if not analysis:
            return None
        return {'summary': analysis.summary, 'mismatches': analysis.mismatches}
