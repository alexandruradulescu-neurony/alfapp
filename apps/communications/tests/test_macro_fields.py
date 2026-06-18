"""Tests for macro_fields() — the Zendesk-macro placeholder extractor.

Follows strict TDD: these tests are written BEFORE the implementation.
They must fail (ImportError or assertion failures) until the implementation
in apps/communications/client_update_templates.py is in place.
"""
import pytest

from apps.integrations.services import (
    ZENDESK_FIELD_AIRPORT,
    ZENDESK_FIELD_AIRLINE,
    ZENDESK_FIELD_FLIGHT,
    ZENDESK_FIELD_DATETIME,
    ZENDESK_FIELD_CLAIM_NUMBER,
    ZENDESK_FIELD_PHONE,
)
from apps.communications.client_update_templates import macro_fields


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claim(**kwargs):
    """Minimal in-memory stand-in for a Claim — no DB required."""
    defaults = dict(
        client_name='',
        object_description='',
        alf_claim_id='',
        phone='',
        flight_data={},
    )
    defaults.update(kwargs)

    class FakeClaim:
        pass

    obj = FakeClaim()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _ticket_data(*pairs):
    """Build a ticket_data dict with a custom_fields list.

    pairs: (field_id, value) tuples — same shape that _get_custom_field_value expects.
    """
    return {
        'custom_fields': [
            {'id': fid, 'value': val}
            for fid, val in pairs
        ]
    }


# ---------------------------------------------------------------------------
# first_name
# ---------------------------------------------------------------------------

class TestFirstName:
    def test_extracts_first_token(self):
        claim = _claim(client_name='Jane Q. Public')
        result = macro_fields(claim)
        assert result['first_name'] == 'Jane'

    def test_single_word_name(self):
        claim = _claim(client_name='Alice')
        assert macro_fields(claim)['first_name'] == 'Alice'

    def test_empty_client_name(self):
        claim = _claim(client_name='')
        assert macro_fields(claim)['first_name'] == ''

    def test_none_client_name(self):
        claim = _claim(client_name=None)
        assert macro_fields(claim)['first_name'] == ''

    def test_whitespace_only_client_name(self):
        claim = _claim(client_name='   ')
        assert macro_fields(claim)['first_name'] == ''


# ---------------------------------------------------------------------------
# lost_item (first line of object_description)
# ---------------------------------------------------------------------------

class TestLostItem:
    def test_single_line_description(self):
        claim = _claim(object_description='Red backpack')
        assert macro_fields(claim)['lost_item'] == 'Red backpack'

    def test_multiline_uses_first_line(self):
        claim = _claim(object_description='Blue laptop\nExtra detail here')
        assert macro_fields(claim)['lost_item'] == 'Blue laptop'

    def test_empty_description(self):
        claim = _claim(object_description='')
        assert macro_fields(claim)['lost_item'] == ''

    def test_none_description(self):
        claim = _claim(object_description=None)
        assert macro_fields(claim)['lost_item'] == ''


# ---------------------------------------------------------------------------
# claim_ref and phone — straight from claim fields
# ---------------------------------------------------------------------------

class TestClaimRefAndPhone:
    def test_claim_ref_from_alf_claim_id(self):
        claim = _claim(alf_claim_id='ALF1234567')
        assert macro_fields(claim)['claim_ref'] == 'ALF1234567'

    def test_claim_ref_empty_when_missing(self):
        claim = _claim(alf_claim_id='')
        assert macro_fields(claim)['claim_ref'] == ''

    def test_claim_ref_none_becomes_empty(self):
        claim = _claim(alf_claim_id=None)
        assert macro_fields(claim)['claim_ref'] == ''

    def test_phone_from_claim(self):
        claim = _claim(phone='+1-800-555-0100')
        assert macro_fields(claim)['phone'] == '+1-800-555-0100'

    def test_phone_empty_when_missing(self):
        claim = _claim(phone='')
        assert macro_fields(claim)['phone'] == ''

    def test_phone_none_becomes_empty(self):
        claim = _claim(phone=None)
        assert macro_fields(claim)['phone'] == ''


# ---------------------------------------------------------------------------
# airport / airline / flight / flight_date from ticket_data custom fields
# ---------------------------------------------------------------------------

class TestTicketDataFields:
    def test_airport_from_ticket_data(self):
        td = _ticket_data((ZENDESK_FIELD_AIRPORT, 'BOS'))
        claim = _claim()
        assert macro_fields(claim, td)['airport'] == 'BOS'

    def test_airline_from_ticket_data(self):
        td = _ticket_data((ZENDESK_FIELD_AIRLINE, 'Delta Air Lines'))
        claim = _claim()
        assert macro_fields(claim, td)['airline'] == 'Delta Air Lines'

    def test_flight_from_ticket_data(self):
        td = _ticket_data((ZENDESK_FIELD_FLIGHT, 'DL123'))
        claim = _claim()
        assert macro_fields(claim, td)['flight'] == 'DL123'

    def test_flight_date_from_ticket_data(self):
        td = _ticket_data((ZENDESK_FIELD_DATETIME, '2026-07-01 14:30'))
        claim = _claim()
        assert macro_fields(claim, td)['flight_date'] == '2026-07-01 14:30'

    def test_multiple_fields_in_single_call(self):
        td = _ticket_data(
            (ZENDESK_FIELD_AIRPORT, 'JFK'),
            (ZENDESK_FIELD_AIRLINE, 'American Airlines'),
            (ZENDESK_FIELD_FLIGHT, 'AA456'),
            (ZENDESK_FIELD_DATETIME, '2026-08-15'),
        )
        claim = _claim()
        result = macro_fields(claim, td)
        assert result['airport'] == 'JFK'
        assert result['airline'] == 'American Airlines'
        assert result['flight'] == 'AA456'
        assert result['flight_date'] == '2026-08-15'


# ---------------------------------------------------------------------------
# Fallback to claim.flight_data when ticket_data absent
# ---------------------------------------------------------------------------

class TestFlightDataFallback:
    def test_airline_falls_back_to_claim_flight_data(self):
        claim = _claim(flight_data={'airline': 'United Airlines', 'number': 'UA789'})
        result = macro_fields(claim, ticket_data=None)
        assert result['airline'] == 'United Airlines'

    def test_flight_falls_back_to_claim_flight_data(self):
        claim = _claim(flight_data={'airline': 'United Airlines', 'number': 'UA789'})
        result = macro_fields(claim, ticket_data=None)
        assert result['flight'] == 'UA789'

    def test_airport_no_fallback_without_ticket_data(self):
        # airport has no claim-level fallback
        claim = _claim(flight_data={'airline': 'United Airlines', 'number': 'UA789'})
        assert macro_fields(claim, ticket_data=None)['airport'] == ''

    def test_flight_date_no_fallback_without_ticket_data(self):
        # flight_date has no claim-level fallback
        claim = _claim(flight_data={'number': 'UA789'})
        assert macro_fields(claim, ticket_data=None)['flight_date'] == ''

    def test_ticket_data_wins_over_claim_fallback(self):
        td = _ticket_data(
            (ZENDESK_FIELD_AIRLINE, 'Delta from ticket'),
            (ZENDESK_FIELD_FLIGHT, 'DL999 from ticket'),
        )
        claim = _claim(flight_data={'airline': 'claim airline', 'number': 'claim flight'})
        result = macro_fields(claim, td)
        assert result['airline'] == 'Delta from ticket'
        assert result['flight'] == 'DL999 from ticket'


# ---------------------------------------------------------------------------
# No flight_data at all — nothing raises
# ---------------------------------------------------------------------------

class TestNoFlightData:
    def test_no_flight_data_and_no_ticket_data_all_empty(self):
        claim = _claim(flight_data={})
        result = macro_fields(claim, ticket_data=None)
        assert result['airport'] == ''
        assert result['airline'] == ''
        assert result['flight'] == ''
        assert result['flight_date'] == ''

    def test_flight_data_missing_keys_no_keyerror(self):
        # flight_data present but without 'airline' or 'number'
        claim = _claim(flight_data={'seat': '12A'})
        result = macro_fields(claim, ticket_data=None)
        assert result['airline'] == ''
        assert result['flight'] == ''

    def test_flight_data_none_no_exception(self):
        claim = _claim(flight_data=None)
        result = macro_fields(claim, ticket_data=None)
        assert result['airline'] == ''
        assert result['flight'] == ''


# ---------------------------------------------------------------------------
# Robustness: malformed / missing ticket_data structures
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_ticket_data_none_does_not_raise(self):
        claim = _claim()
        result = macro_fields(claim, None)  # must not raise
        assert isinstance(result, dict)

    def test_ticket_data_missing_custom_fields_key(self):
        claim = _claim()
        result = macro_fields(claim, {})  # no 'custom_fields' key
        assert result['airport'] == ''

    def test_ticket_data_custom_fields_none(self):
        claim = _claim()
        result = macro_fields(claim, {'custom_fields': None})
        assert result['flight'] == ''

    def test_ticket_data_custom_fields_empty_list(self):
        claim = _claim()
        result = macro_fields(claim, {'custom_fields': []})
        assert result['airline'] == ''

    def test_all_keys_present_in_result(self):
        """The dict must always contain all 8 expected keys."""
        claim = _claim()
        result = macro_fields(claim)
        expected_keys = {'first_name', 'lost_item', 'airport', 'airline',
                         'flight', 'flight_date', 'claim_ref', 'phone'}
        assert set(result.keys()) == expected_keys
