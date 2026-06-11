"""Tests for the flight lookup feature (service module + endpoint).

External boundaries are mocked: AeroDataBox via _aerodatabox_get / urlopen,
AI via AIClient.complete, Zendesk notes via post_zendesk_comment.
"""
import urllib.error
from datetime import time as dt_time
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.ai.schemas import FlightCheck
from apps.claims.models import Claim
from apps.config.models import SystemSettings

SECRET = 'flight-test-secret'

COMPOSED = ('Flight: RO301 | Airline: TAROM | Airport: Henri Coanda (OTP) | '
            'Seat: 12A | Date/Time: 2026-06-01 14:20')

RAW_LEG = {
    'number': 'RO 301',
    'status': 'Arrived',
    'airline': {'name': 'TAROM'},
    'departure': {
        'airport': {'iata': 'OTP', 'name': 'Henri Coanda', 'municipalityName': 'Bucharest'},
        'scheduledTime': {'local': '2026-06-01 14:20+03:00'},
    },
    'arrival': {
        'airport': {'iata': 'CDG', 'name': 'Charles de Gaulle', 'municipalityName': 'Paris'},
        'scheduledTime': {'local': '2026-06-01 17:05+02:00'},
    },
}


def _fake_check(summary='Route matches the client report.', mismatches=None):
    return FlightCheck(summary=summary, mismatches=mismatches or [])


class ParseFlightQueryTests(TestCase):
    def test_labeled_string(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertEqual(parse_flight_query(COMPOSED),
                         {'number': 'RO301', 'date': '2026-06-01'})

    def test_missing_date_returns_none(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertIsNone(parse_flight_query('Flight: RO301 | Airline: TAROM'))

    def test_missing_number_returns_none(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertIsNone(parse_flight_query('Date/Time: 2026-06-01 14:20'))

    def test_garbage_returns_none(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertIsNone(parse_flight_query('forgot my wallet at the gate'))

    def test_bare_string_fallback(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertEqual(parse_flight_query('W6 3001 on 2026-06-02 to Rome'),
                         {'number': 'W63001', 'date': '2026-06-02'})


class ParseHintsTests(TestCase):
    def test_airport_from_parentheses(self):
        from apps.integrations.flight_lookup import parse_airport_hint
        self.assertEqual(parse_airport_hint(COMPOSED), 'OTP')

    def test_airport_bare_code(self):
        from apps.integrations.flight_lookup import parse_airport_hint
        self.assertEqual(parse_airport_hint('Airport: JFK New York'), 'JFK')

    def test_airport_stopword_skipped(self):
        from apps.integrations.flight_lookup import parse_airport_hint
        self.assertIsNone(parse_airport_hint('Airport: New Airfield'))

    def test_no_airport_segment(self):
        from apps.integrations.flight_lookup import parse_airport_hint
        self.assertIsNone(parse_airport_hint('Flight: RO301'))

    def test_time_hint(self):
        from apps.integrations.flight_lookup import parse_time_hint
        self.assertEqual(parse_time_hint(COMPOSED), dt_time(14, 20))

    def test_time_hint_missing(self):
        from apps.integrations.flight_lookup import parse_time_hint
        self.assertIsNone(parse_time_hint('Date/Time: 2026-06-01'))


class NormalizeFlightTests(TestCase):
    def test_normalizes_legs_and_header(self):
        from apps.integrations.flight_lookup import normalize_flight
        result = normalize_flight([RAW_LEG])
        self.assertEqual(result['number'], 'RO 301')
        self.assertEqual(result['airline'], 'TAROM')
        self.assertEqual(result['status'], 'Arrived')
        self.assertEqual(len(result['legs']), 1)
        leg = result['legs'][0]
        self.assertEqual(leg['from_iata'], 'OTP')
        self.assertEqual(leg['to_city'], 'Paris')
        self.assertTrue(result['looked_up_at'])

    def test_defensive_on_sparse_payload(self):
        from apps.integrations.flight_lookup import normalize_flight
        result = normalize_flight([{'number': 'RO301'}])
        self.assertEqual(result['legs'][0]['from_iata'], '')


class LookupFlightTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.aerodatabox_api_key = 'k'
        ss.save()

    @patch('apps.integrations.flight_lookup._aerodatabox_get', return_value=[RAW_LEG])
    def test_success_returns_list(self, _mock):
        from apps.integrations.flight_lookup import lookup_flight
        self.assertEqual(lookup_flight('RO301', '2026-06-01'), [RAW_LEG])

    @patch('apps.integrations.flight_lookup._aerodatabox_get',
           side_effect=urllib.error.HTTPError('u', 404, 'nf', {}, None))
    def test_404_means_not_found(self, _mock):
        from apps.integrations.flight_lookup import lookup_flight
        self.assertEqual(lookup_flight('RO301', '2026-06-01'), [])

    @patch('apps.integrations.flight_lookup._aerodatabox_get',
           side_effect=urllib.error.URLError('down'))
    def test_transport_error_returns_none(self, _mock):
        from apps.integrations.flight_lookup import lookup_flight
        self.assertIsNone(lookup_flight('RO301', '2026-06-01'))

    def test_missing_key_raises(self):
        from apps.integrations.flight_lookup import (
            FlightProviderNotConfigured, lookup_flight)
        ss = SystemSettings.get_instance()
        ss.aerodatabox_api_key = ''
        ss.save()
        with self.assertRaises(FlightProviderNotConfigured):
            lookup_flight('RO301', '2026-06-01')


class FindCandidateFlightsTests(TestCase):
    DEPARTURES = {'departures': [
        {'number': 'RO301',
         'movement': {'airport': {'iata': 'CDG', 'name': 'Charles de Gaulle'},
                      'scheduledTime': {'local': '2026-06-01 14:20+03:00'}}},
        {'number': 'RO307',
         'movement': {'airport': {'iata': 'ORY', 'name': 'Orly'},
                      'scheduledTime': {'local': '2026-06-01 15:05+03:00'}}},
    ]}

    @patch('apps.integrations.flight_lookup._aerodatabox_get')
    def test_candidates_normalized_and_window_from_hint(self, mock_get):
        from apps.integrations.flight_lookup import find_candidate_flights
        mock_get.return_value = self.DEPARTURES
        result = find_candidate_flights('OTP', '2026-06-01', dt_time(14, 20))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['number'], 'RO301')
        self.assertIn('CDG', result[0]['destination'])
        path = mock_get.call_args.args[0]
        self.assertIn('/flights/airports/iata/OTP/2026-06-01T11:00/2026-06-01T17:59', path)

    @patch('apps.integrations.flight_lookup._aerodatabox_get')
    def test_destination_filter(self, mock_get):
        from apps.integrations.flight_lookup import find_candidate_flights
        mock_get.return_value = self.DEPARTURES
        result = find_candidate_flights('OTP', '2026-06-01', None, destination_hint='Orly')
        self.assertEqual([c['number'] for c in result], ['RO307'])

    @patch('apps.integrations.flight_lookup._aerodatabox_get',
           side_effect=urllib.error.URLError('down'))
    def test_transport_error_returns_none(self, _mock):
        from apps.integrations.flight_lookup import find_candidate_flights
        self.assertIsNone(find_candidate_flights('OTP', '2026-06-01'))


class AnalyzeFlightMatchTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='fl@example.com', client_name='Ana Pop',
            zd_ticket_id='80001', flight_details=COMPOSED,
            lost_location='Gate 12 security', incident_details='Left it at security around 13:40')

    @patch('apps.integrations.flight_lookup.AIClient.complete')
    def test_channels_and_pii(self, mock_complete):
        from apps.integrations.flight_lookup import analyze_flight_match, normalize_flight
        mock_complete.return_value = _fake_check()
        flight = normalize_flight([RAW_LEG])
        result = analyze_flight_match(self.claim, flight)
        self.assertEqual(result.summary, 'Route matches the client report.')
        kwargs = mock_complete.call_args.kwargs
        self.assertIn('CDG', str(kwargs['trusted']))           # flight data is trusted
        self.assertIn('Gate 12', str(kwargs['untrusted']))      # client text is untrusted
        self.assertNotIn('Gate 12', str(kwargs['trusted']))
        self.assertIn('Ana Pop', kwargs['known_pii']['names'])
        self.assertEqual(kwargs['call_site'], 'flight_check')

    @patch('apps.integrations.flight_lookup.AIClient.complete', side_effect=RuntimeError('down'))
    def test_ai_failure_returns_none(self, _mock):
        from apps.integrations.flight_lookup import analyze_flight_match
        self.assertIsNone(analyze_flight_match(self.claim, {'number': 'RO301', 'legs': []}))


class FormatNotesTests(TestCase):
    def test_found_note_with_analysis_and_verdict(self):
        from apps.integrations.flight_lookup import (
            derive_flight_verdict, format_flight_note, normalize_flight)
        analysis = _fake_check(mismatches=['Client selected OTP; loss after landing at CDG'])
        verdict = derive_flight_verdict(True, analysis)
        note = format_flight_note(normalize_flight([RAW_LEG]), analysis, verdict)
        first_line = note.splitlines()[0]
        self.assertIn('verify details', first_line)        # verdict leads the note
        self.assertIn('⚠️', first_line)
        self.assertIn('Flight RO 301', note)
        self.assertIn('OTP (Bucharest) 14:20 → CDG (Paris) 17:05', note)
        self.assertIn('AI check:', note)
        self.assertIn('• Client selected OTP', note)

    def test_multi_leg_note_labels_legs(self):
        from apps.integrations.flight_lookup import format_flight_note, normalize_flight
        leg2 = {
            'number': 'RO 301', 'status': 'Expected',
            'departure': {'airport': {'iata': 'CDG', 'name': 'Charles de Gaulle',
                                      'municipalityName': 'Paris'},
                          'scheduledTime': {'local': '2026-06-01 19:10+02:00'}},
            'arrival': {'airport': {'iata': 'OTP', 'name': 'Henri Coanda',
                                    'municipalityName': 'Bucharest'},
                        'scheduledTime': {'local': '2026-06-01 22:55+03:00'}},
        }
        note = format_flight_note(normalize_flight([dict(RAW_LEG, status='Arrived'), leg2]), None)
        self.assertIn('Leg 1: OTP (Bucharest) 14:20 → CDG (Paris) 17:05 — Arrived', note)
        self.assertIn('Leg 2: CDG (Paris) 19:10 → OTP (Bucharest) 22:55 — Expected', note)

    def test_candidates_note(self):
        from apps.integrations.flight_lookup import (
            derive_flight_verdict, format_candidates_note)
        verdict = derive_flight_verdict(False, None, has_candidates=True)
        note = format_candidates_note('RO3O1', '2026-06-01', 'OTP',
                                      [{'number': 'RO301', 'destination': 'CDG Charles de Gaulle',
                                        'scheduled_local': '2026-06-01 14:20+03:00'}], None, verdict)
        self.assertIn('❌', note.splitlines()[0])
        self.assertIn('RO3O1 not found', note)
        self.assertIn('• RO301 → CDG Charles de Gaulle dep 14:20', note)

    def test_not_found_note(self):
        from apps.integrations.flight_lookup import (
            derive_flight_verdict, format_not_found_note)
        note = format_not_found_note('RO301', '2026-06-01',
                                     derive_flight_verdict(False, None))
        self.assertIn('❌ Flight NOT found', note.splitlines()[0])
        self.assertIn('Flight information was not found for RO301 on 2026-06-01.', note)


class DeriveFlightVerdictTests(TestCase):
    def test_verified_when_found_and_clean(self):
        from apps.integrations.flight_lookup import derive_flight_verdict
        verdict = derive_flight_verdict(True, _fake_check(mismatches=[]))
        self.assertEqual(verdict['level'], 'verified')

    def test_check_when_mismatches_flagged(self):
        from apps.integrations.flight_lookup import derive_flight_verdict
        verdict = derive_flight_verdict(True, _fake_check(mismatches=['wrong day']))
        self.assertEqual(verdict['level'], 'check')

    def test_unchecked_when_ai_failed(self):
        from apps.integrations.flight_lookup import derive_flight_verdict
        verdict = derive_flight_verdict(True, None)
        self.assertEqual(verdict['level'], 'unchecked')
        self.assertIn('verify manually', verdict['label'])

    def test_not_found_with_and_without_candidates(self):
        from apps.integrations.flight_lookup import derive_flight_verdict
        self.assertEqual(derive_flight_verdict(False, None)['level'], 'not_found')
        self.assertIn('candidates',
                      derive_flight_verdict(False, None, has_candidates=True)['label'])


class FlightLookupEndpointTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.aerodatabox_api_key = 'k'
        ss.save()
        self.api = APIClient()
        self.claim = Claim.objects.create(
            client_email='ep@example.com', client_name='Ana Pop',
            zd_ticket_id='90001', flight_details=COMPOSED)
        self.url = '/api/integrations/zd/flight-lookup/'

    def _post(self, body=None):
        return self.api.post(self.url, body or {'ticket_id': '90001'},
                             format='json', HTTP_AUTHORIZATION=f'Bearer {SECRET}')

    def test_auth_required(self):
        response = self.api.post(self.url, {'ticket_id': '90001'}, format='json')
        self.assertEqual(response.status_code, 403)

    # NOTE: "no claim -> refuse" was removed 2026-06-11; claimless tickets now
    # fall back to the structured Zendesk fields (see ClaimlessFlightLookupTests).

    def test_unparseable_flight(self):
        self.claim.flight_details = 'no flight info here'
        self.claim.save()
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Couldn't read", response.json()['error_message'])

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.analyze_flight_match', return_value=_fake_check())
    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    def test_success_saves_posts_and_returns(self, mock_lookup, mock_analyze, mock_post):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['flight']['number'], 'RO 301')
        self.assertFalse(data['cached'])
        self.assertTrue(data['note_posted'])
        self.assertEqual(data['analysis']['summary'], 'Route matches the client report.')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.flight_data['number'], 'RO 301')
        self.assertIsNotNone(self.claim.flight_data_updated_at)
        entry = self.claim.updates.first()
        self.assertEqual(entry.update_type, 'INFO_UPDATED')
        self.assertIn('flight_lookup', entry.changes_summary)
        note_body = mock_post.call_args.args[1]
        self.assertIn('AI check:', note_body)

    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    @patch('apps.integrations.views.analyze_flight_match', return_value=None)
    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    def test_analysis_failure_still_succeeds(self, mock_post, _mock_analyze, _mock_lookup):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['analysis'])
        note_body = mock_post.call_args.args[1]
        self.assertNotIn('AI check:', note_body)

    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    @patch('apps.integrations.views.analyze_flight_match', return_value=_fake_check())
    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    def test_cached_second_call_skips_api(self, _mock_post, _mock_analyze, mock_lookup):
        self._post()
        self.assertEqual(mock_lookup.call_count, 1)
        response = self._post()
        self.assertEqual(mock_lookup.call_count, 1)  # not called again
        self.assertTrue(response.json()['cached'])

    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    @patch('apps.integrations.views.analyze_flight_match', return_value=_fake_check())
    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    def test_refresh_forces_new_lookup(self, _mock_post, _mock_analyze, mock_lookup):
        self._post()
        response = self._post({'ticket_id': '90001', 'refresh': True})
        self.assertEqual(mock_lookup.call_count, 2)
        self.assertFalse(response.json()['cached'])

    @patch('apps.integrations.views.lookup_flight', return_value=None)
    def test_provider_down_502_no_note(self, _mock_lookup):
        with patch('apps.integrations.views.post_zendesk_comment') as mock_post:
            response = self._post()
        self.assertEqual(response.status_code, 502)
        mock_post.assert_not_called()

    @patch('apps.integrations.views.lookup_flight',
           side_effect=__import__('apps.integrations.flight_lookup', fromlist=['FlightProviderNotConfigured']).FlightProviderNotConfigured('no key'))
    def test_missing_key_503(self, _mock_lookup):
        response = self._post()
        self.assertEqual(response.status_code, 503)
        self.assertIn('not configured', response.json()['error'])

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.analyze_flight_match', return_value=_fake_check('RO301 at 14:20 fits best.'))
    @patch('apps.integrations.views.find_candidate_flights',
           return_value=[{'number': 'RO301', 'destination': 'CDG Charles de Gaulle',
                          'scheduled_local': '2026-06-01 14:20+03:00'}])
    @patch('apps.integrations.views.lookup_flight', return_value=[])
    def test_not_found_with_candidates(self, _ml, mock_candidates, _ma, mock_post):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('No flight found', data['error_message'])
        self.assertEqual(len(data['candidates']), 1)
        self.assertTrue(data['note_posted'])
        note_body = mock_post.call_args.args[1]
        self.assertIn('likely candidates', note_body)
        entry = self.claim.updates.first()
        self.assertIn('"found": false', entry.changes_summary)

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.find_candidate_flights', return_value=[])
    @patch('apps.integrations.views.lookup_flight', return_value=[])
    def test_not_found_plain_note(self, _ml, _mc, mock_post):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('candidates', response.json())
        note_body = mock_post.call_args.args[1]
        self.assertIn('was not found', note_body)

    @patch('apps.integrations.views.post_zendesk_comment', side_effect=RuntimeError('zd down'))
    @patch('apps.integrations.views.analyze_flight_match', return_value=None)
    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    def test_note_failure_tolerated(self, _ml, _ma, _mp):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['note_posted'])


class NotFoundSignalTests(TestCase):
    """AeroDataBox signals 'no data' with HTTP 204 + empty body; _aerodatabox_get
    maps that to None and both callers must treat it as not-found, not outage."""

    @patch('apps.integrations.flight_lookup._aerodatabox_get', return_value=None)
    def test_lookup_204_means_not_found(self, _mock):
        from apps.integrations.flight_lookup import lookup_flight
        self.assertEqual(lookup_flight('RO301', '2026-06-01'), [])

    @patch('apps.integrations.flight_lookup._aerodatabox_get', return_value=None)
    def test_candidates_204_means_no_candidates(self, _mock):
        from apps.integrations.flight_lookup import find_candidate_flights
        self.assertEqual(find_candidate_flights('OTP', '2026-06-01'), [])

    @patch('apps.integrations.flight_lookup.urllib.request.urlopen')
    def test_get_returns_none_on_204_empty_body(self, mock_urlopen):
        from apps.integrations.flight_lookup import _aerodatabox_get
        ss = SystemSettings.get_instance()
        ss.aerodatabox_api_key = 'k'
        ss.save()
        response = mock_urlopen.return_value.__enter__.return_value
        response.status = 204
        response.read.return_value = b''
        self.assertIsNone(_aerodatabox_get('/flights/number/RO301/2026-06-01'))


class FidsWindowTests(TestCase):
    @patch('apps.integrations.flight_lookup._aerodatabox_get', return_value={'departures': []})
    def test_default_window_stays_under_12_hours(self, mock_get):
        from apps.integrations.flight_lookup import find_candidate_flights
        find_candidate_flights('OTP', '2026-06-01', None)
        path = mock_get.call_args.args[0]
        self.assertIn('/2026-06-01T08:00/2026-06-01T19:59', path)


class TimeHintIsoTests(TestCase):
    def test_iso_t_separated_datetime(self):
        from apps.integrations.flight_lookup import parse_time_hint
        self.assertEqual(parse_time_hint('Date/Time: 2026-06-01T14:20:00'),
                         dt_time(14, 20))


class HumanDateFormatTests(TestCase):
    """Zendesk's 'Date & Time' form field arrives as human English in the wild
    (seen in production: 'June 11, 2026 9:15 am') — not ISO."""

    TAMPA = ('Flight: DL2852 | Airline: Delta Air Lines - DL | '
             'Airport: Tampa International Airport / TPA | '
             'Date/Time: June 11, 2026 9:15 am')

    def test_real_world_tampa_claim(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertEqual(parse_flight_query(self.TAMPA),
                         {'number': 'DL2852', 'date': '2026-06-11'})

    def test_day_first_english(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertEqual(
            parse_flight_query('Flight: RO301 | Date/Time: 11 June 2026'),
            {'number': 'RO301', 'date': '2026-06-11'})

    def test_us_slash_date(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertEqual(
            parse_flight_query('Flight: RO301 | Date/Time: 06/11/2026 9:15'),
            {'number': 'RO301', 'date': '2026-06-11'})

    def test_slash_date_day_first_when_over_12(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertEqual(
            parse_flight_query('Flight: RO301 | Date/Time: 23/06/2026'),
            {'number': 'RO301', 'date': '2026-06-23'})

    def test_pm_time_hint(self):
        from apps.integrations.flight_lookup import parse_time_hint
        self.assertEqual(parse_time_hint('Date/Time: June 11, 2026 9:15 pm'),
                         dt_time(21, 15))

    def test_am_time_hint_from_tampa(self):
        from apps.integrations.flight_lookup import parse_time_hint
        self.assertEqual(parse_time_hint(self.TAMPA), dt_time(9, 15))

    def test_12am_time_hint(self):
        from apps.integrations.flight_lookup import parse_time_hint
        self.assertEqual(parse_time_hint('Date/Time: 12:30 am'), dt_time(0, 30))

    def test_airport_hint_from_tampa(self):
        from apps.integrations.flight_lookup import parse_airport_hint
        self.assertEqual(parse_airport_hint(self.TAMPA), 'TPA')


class UserAgentHeaderTests(TestCase):
    """RapidAPI's edge 403s Python's default urllib User-Agent (verified live);
    every AeroDataBox request must carry our own UA."""

    @patch('apps.integrations.flight_lookup.urllib.request.urlopen')
    def test_requests_carry_custom_user_agent(self, mock_urlopen):
        from apps.integrations.flight_lookup import _aerodatabox_get
        ss = SystemSettings.get_instance()
        ss.aerodatabox_api_key = 'k'
        ss.save()
        response = mock_urlopen.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'[]'
        _aerodatabox_get('/flights/number/RO301/2026-06-01')
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'), 'LORA-flight-lookup/1.0')


class VerdictInEndpointTests(FlightLookupEndpointTests):
    """The verdict travels in the response AND is stored inside flight_data
    (so cached responses keep it)."""

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.analyze_flight_match', return_value=_fake_check())
    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    def test_success_response_and_stored_verdict(self, *_mocks):
        response = self._post()
        data = response.json()
        self.assertEqual(data['verdict']['level'], 'verified')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.flight_data['verdict']['level'], 'verified')

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.analyze_flight_match',
           return_value=_fake_check(mismatches=['airport not on route']))
    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    def test_mismatch_gives_check_verdict_and_leads_note(self, _ml, _ma, mock_post):
        response = self._post()
        self.assertEqual(response.json()['verdict']['level'], 'check')
        note_body = mock_post.call_args.args[1]
        self.assertIn('⚠️', note_body.splitlines()[0])


class ClaimlessFlightLookupTests(TestCase):
    """No LORA claim: flight details come from the ticket's structured
    Zendesk fields (server-side fetch). Nothing is cached, no timeline rows
    are written — the internal note is the record."""

    TICKET_FIELDS = {'custom_fields': [
        {'id': 13737630819996, 'value': 'DL2852'},               # Flight Number
        {'id': 11761080032028, 'value': 'Delta Air Lines - DL'}, # Airline
        {'id': 11761104069276, 'value': 'Tampa International Airport / TPA'},
        {'id': 13737598795292, 'value': 'June 11, 2026 9:15 am'},  # Date & Time
    ]}

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.aerodatabox_api_key = 'k'
        ss.save()
        self.api = APIClient()
        self.url = '/api/integrations/zd/flight-lookup/'

    def _post(self, ticket_id='95001'):
        return self.api.post(self.url, {'ticket_id': ticket_id},
                             format='json', HTTP_AUTHORIZATION=f'Bearer {SECRET}')

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.analyze_flight_match', return_value=_fake_check())
    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    @patch('apps.integrations.views.fetch_zendesk_ticket')
    def test_claimless_lookup_from_ticket_fields(self, mock_fetch, mock_lookup,
                                                 mock_analyze, mock_post):
        from apps.claims.models import ClaimUpdateTimeline
        timeline_before = ClaimUpdateTimeline.objects.count()  # shared fixtures seed rows
        mock_fetch.return_value = dict(self.TICKET_FIELDS)
        response = self._post()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['claimless'])
        self.assertEqual(data['flight']['number'], 'RO 301')
        self.assertEqual(data['verdict']['level'], 'verified')
        self.assertTrue(data['note_posted'])
        # the lookup used the fields-composed details
        mock_lookup.assert_called_once_with('DL2852', '2026-06-11')
        # note went to THIS ticket
        self.assertEqual(mock_post.call_args.args[0], '95001')
        # nothing persisted anywhere (relative: shared fixtures pre-seed rows)
        self.assertEqual(ClaimUpdateTimeline.objects.count(), timeline_before)
        # claimless analysis got the composed details as client text
        kwargs = mock_analyze.call_args.kwargs
        self.assertIn('DL2852', kwargs['flight_details_text'])

    @patch('apps.integrations.views.fetch_zendesk_ticket',
           return_value={'custom_fields': []})
    def test_claimless_empty_fields_message(self, _mock_fetch):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIn('flight fields', response.json()['error_message'])

    @patch('apps.integrations.views.fetch_zendesk_ticket', return_value=None)
    def test_claimless_ticket_fetch_failure_message(self, _mock_fetch):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Couldn't read this ticket's fields", response.json()['error_message'])

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.analyze_flight_match', return_value=_fake_check())
    @patch('apps.integrations.views.lookup_flight', return_value=[RAW_LEG])
    @patch('apps.integrations.views.fetch_zendesk_ticket')
    def test_claimless_is_never_cached(self, mock_fetch, mock_lookup, *_mocks):
        mock_fetch.return_value = dict(self.TICKET_FIELDS)
        self._post()
        self._post()
        self.assertEqual(mock_lookup.call_count, 2)


class ClaimlessAnalysisChannelTests(TestCase):
    def test_claimless_analysis_uses_untrusted_channel_without_claim_facts(self):
        from apps.integrations.flight_lookup import analyze_flight_match
        with patch('apps.integrations.flight_lookup.AIClient.complete') as mock_complete:
            mock_complete.return_value = _fake_check()
            result = analyze_flight_match(None, {'number': 'DL2852', 'legs': []},
                                          flight_details_text=COMPOSED)
            self.assertIsNotNone(result)
            kwargs = mock_complete.call_args.kwargs
            self.assertIn('RO301', str(kwargs['untrusted']))
            self.assertNotIn('claim_facts', kwargs['trusted'] or {})
            self.assertEqual(kwargs['known_pii']['names'], [])


class BareFlightNumberTests(TestCase):
    """Clients often type only the digits ('377'); the airline code comes
    from the Airline field (verified live: AA + 377 found BOS->PHX)."""

    BOSTON = ('Flight: 377 | Airline: American Airlines - AA | '
              'Airport: Logan International Airport / BOS | '
              'Date/Time: June 11, 2026 8:15 am')

    def test_bare_number_borrows_airline_code(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertEqual(parse_flight_query(self.BOSTON),
                         {'number': 'AA377', 'date': '2026-06-11'})

    def test_airline_code_variants(self):
        from apps.integrations.flight_lookup import _airline_code
        self.assertEqual(_airline_code('American Airlines - AA'), 'AA')
        self.assertEqual(_airline_code('Wizz Air W6'), 'W6')
        self.assertIsNone(_airline_code('Lufthansa'))

    def test_bare_number_without_airline_code_fails_closed(self):
        from apps.integrations.flight_lookup import parse_flight_query
        self.assertIsNone(parse_flight_query(
            'Flight: 377 | Airline: Lufthansa | Date/Time: 2026-06-11'))


class RateLimitRetryTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.aerodatabox_api_key = 'k'
        ss.save()

    @patch('apps.integrations.flight_lookup.time.sleep')
    @patch('apps.integrations.flight_lookup.urllib.request.urlopen')
    def test_429_retries_once_then_succeeds(self, mock_urlopen, mock_sleep):
        from apps.integrations.flight_lookup import _aerodatabox_get
        ok = mock_urlopen.return_value.__enter__.return_value
        ok.status = 200
        ok.read.return_value = b'[]'
        mock_urlopen.side_effect = [
            urllib.error.HTTPError('u', 429, 'rate', {}, None),
            mock_urlopen.return_value,
        ]
        self.assertEqual(_aerodatabox_get('/flights/number/AA377/2026-06-11'), [])
        mock_sleep.assert_called_once()

    @patch('apps.integrations.flight_lookup.time.sleep')
    @patch('apps.integrations.flight_lookup.urllib.request.urlopen',
           side_effect=urllib.error.HTTPError('u', 429, 'rate', {}, None))
    def test_429_twice_propagates(self, _mock_urlopen, _mock_sleep):
        from apps.integrations.flight_lookup import _aerodatabox_get
        with self.assertRaises(urllib.error.HTTPError):
            _aerodatabox_get('/flights/number/AA377/2026-06-11')


class OldDateHintTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.aerodatabox_api_key = 'k'
        ss.save()
        self.api = APIClient()
        # Old flight, no Airport segment -> rescue skipped, plain not-found.
        self.claim = Claim.objects.create(
            client_email='old-flight@example.com', zd_ticket_id='97001',
            flight_details='Flight: AA377 | Date/Time: 2026-04-01 08:15')

    @patch('apps.integrations.views.post_zendesk_comment', return_value={'ok': True})
    @patch('apps.integrations.views.lookup_flight', return_value=[])
    def test_old_date_not_found_mentions_history_window(self, _ml, _mp):
        response = self.api.post('/api/integrations/zd/flight-lookup/',
                                 {'ticket_id': '97001'}, format='json',
                                 HTTP_AUTHORIZATION=f'Bearer {SECRET}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('history window', response.json()['error_message'])
