"""ircc_processing_times: a spousal sponsorship query returns only spousal rows (OmniSeek showcase R09, 2026-10-11).

R09 asked "spousal sponsorship outland processing time" and got citizenship renunciation plus seven
government-assisted refugee rows, no spousal row at all. Two faults: the adapter never read IRCC's
flpt-en.json, the only payload that carries spousal sponsorship; and the shared keyword filter
admitted any row on one matching word, while every row carries "Current IRCC processing time".
Offline: the tables below are trimmed from the real payloads (data-ptime*, as cached by OmniSeek on
2026-10-11; flpt-en.json as published 2026-09-03), the CDP egress is stubbed.
"""
from __future__ import annotations

import unittest
from unittest import mock

from omniseek.core.sources.api import ircc_processing_times_source as ircc

FLPT = {
    "default-update": {"flpt_lastupdated": "September 3, 2026", "flpt_interval": "monthly"},
    "total-people": {"spousal-abroad-roc": "About 21,400 people waiting",
                     "pgp-roc": "About 40,600 people waiting"},
    "current-flpt": {
        "spousal-canada-quebec": "About 32 months",
        "spousal-canada-roc": "About 26 months",
        "spousal-abroad-quebec": "About 33 months",
        "spousal-abroad-roc": "About 18 months",
        "pgp-roc": "About 28 months",
        "dependants-abroad-roc": "About 41 months",
        "government-refugees-roc": "About 14 months",
        "private-refugees-roc": "About 40 months",
        "citizen-grants": "About 12 months",
        "cec": "About 6 months",
    },
}


def _tables(with_flpt=True):
    t = {
        "country": {
            "study": {"CN": "4 weeks", "IN": "5 weeks"},
            "child_dependent": {"CN": "13 months"},
            "refugees_gov": {"BD": "14 months", "TD": "12 months", "KE": "19 months"},
            "refugees_private": {"KE": {"sponsor": "Not enough data", "refugee": "31 months"}},
        },
        "noncountry": {
            "default-update": {"lastupdated": "October 7, 2026"},
            "citizenship": {"cit_renunciation": "4 months"},
            "pr_card": {"new_pr": "33 days"},
            "visitor_inside_canada": {"visitor_inside_canada": "11 days"},
        },
        "names": {"CN": "China", "IN": "India", "BD": "Bangladesh", "TD": "Chad", "KE": "Kenya"},
    }
    if with_flpt:
        t["flpt"] = FLPT
    return t


def _search(query, tables=None, limit=10):
    ad = ircc.IRCCProcessingTimesAdapter()
    with mock.patch.object(ad, "_tables", return_value=tables or _tables()):
        return ad.search(query, limit=limit)


SPOUSAL = {"flpt:spousal-canada-quebec", "flpt:spousal-canada-roc",
           "flpt:spousal-abroad-quebec", "flpt:spousal-abroad-roc"}


class SpousalQueryTests(unittest.TestCase):
    def test_r09_query_returns_only_the_outland_spousal_rows(self):
        docs = _search("spousal sponsorship outland processing time", limit=8)
        self.assertEqual({d.source_id for d in docs},
                         {"flpt:spousal-abroad-roc", "flpt:spousal-abroad-quebec"})
        roc = next(d for d in docs if d.source_id == "flpt:spousal-abroad-roc")
        self.assertIn("About 18 months", roc.title)
        self.assertEqual(roc.metadata["people_waiting"], "About 21,400 people waiting")

    def test_spouse_sponsorship_returns_the_four_spousal_rows_and_nothing_else(self):
        self.assertEqual({d.source_id for d in _search("spouse sponsorship")}, SPOUSAL)

    def test_partner_synonyms_and_inland_reach_the_spousal_rows(self):
        self.assertEqual({d.source_id for d in _search("common-law partner inland")},
                         {"flpt:spousal-canada-roc", "flpt:spousal-canada-quebec"})

    def test_without_the_flpt_payload_a_spouse_query_is_an_honest_no_match(self):
        self.assertEqual(_search("spousal sponsorship outland processing time",
                                 tables=_tables(with_flpt=False)), [])


class FilterContractTests(unittest.TestCase):
    def test_noise_only_query_is_treated_as_empty(self):
        everything = _search("", limit=100)
        self.assertEqual([d.source_id for d in _search("processing time", limit=100)],
                         [d.source_id for d in everything])

    def test_best_covered_rows_only(self):
        self.assertEqual([d.source_id for d in _search("study permit China")], ["study:CN"])


class FlptRowsTests(unittest.TestCase):
    def test_flpt_rows_carry_their_own_date_and_flags(self):
        docs = {d.source_id: d for d in ircc.IRCCProcessingTimesAdapter()._build_docs(_tables())}
        row = docs["flpt:spousal-canada-roc"]
        self.assertEqual(row.date.date().isoformat(), "2026-09-03")
        self.assertTrue(row.metadata["forward_looking"])
        self.assertTrue(row.metadata["in_canada"])
        self.assertFalse(docs["flpt:spousal-abroad-roc"].metadata["in_canada"])
        self.assertEqual(docs["study:CN"].date.date().isoformat(), "2026-10-07")

    def test_flpt_failure_keeps_the_table_and_caches_it_briefly(self):
        def fetch(url):
            if url == ircc.FLPT_JSON:
                raise RuntimeError("cdp down")
            if url == ircc.COUNTRY_NAME_JSON:
                return {"country-name": {"CN": "China"}}
            return {"study": {"CN": "4 weeks"}} if url == ircc.PTIME_JSON else {}

        with mock.patch.object(ircc, "_fetch_json", side_effect=fetch), \
                mock.patch.object(ircc.cache, "get", return_value=None), \
                mock.patch.object(ircc.cache, "set") as cset:
            tables = ircc.IRCCProcessingTimesAdapter()._tables()
        self.assertIsNone(tables["flpt"])
        self.assertEqual(tables["country"], {"study": {"CN": "4 weeks"}})
        self.assertEqual(cset.call_args.kwargs["ttl"], ircc.PARTIAL_CACHE_TTL)


if __name__ == "__main__":
    unittest.main()
