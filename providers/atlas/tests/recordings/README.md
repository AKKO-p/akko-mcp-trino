# Recorded Atlas answers

Each file is one exchange with Atlas, `{"request": {...}, "response": {...}}`, replayed by the
tests in place of a catalogue. Only reads were recorded: `GET`, and
`POST /api/atlas/v2/search/basic`. Response headers were not kept.

- `atlas-2.1`: a vendor build of Atlas 2.1.
- `atlas-2.4`: Apache Atlas 2.4.0, built from the official sources, holding a small catalogue
  written for these tests.

These are not raw answers. `anonymize.py` made them neutral, keeping their structure:

- every name (database, table, column, glossary, term, category, classification, business
  metadata set and attribute, cluster, user) is replaced by a neutral one, the same everywhere;
- every free text reads `<field> of <object>`;
- every GUID is sequential, every time is fixed, the build revision is zeros and the version
  is cut to `major.minor.patch`;
- table parameters (DDL times, row counts, sizes) are emptied, and the statistics a vendor's
  types add (file and snapshot counts, snapshot ids) are set to null.

The 2.1 answers were changed further, by hand:

- business attributes were removed: tables keep `stewardship.approvalStatus` and
  `stewardship.ownerTeam`, columns and terms keep `approvalStatus` only;
- the glossary listing was rewritten: it announces 32 terms, in pages at offsets 0, 10 and 23
  (the gap is not recorded), and its last page holds every term the other recordings name.

`test_recordings.py` checks that the recordings agree with one another; `anonymize.py` says how
to add a version.
