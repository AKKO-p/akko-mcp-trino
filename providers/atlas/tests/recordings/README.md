# Recorded Atlas answers

Each file is one exchange with Atlas, `{"request": {...}, "response": {...}}`, replayed by the
tests in place of a catalogue. Only reads were recorded: `GET`, and
`POST /api/atlas/v2/search/basic`. Response headers were not kept.

- `atlas-2.1-seed`: a build of Atlas 2.1, the version of reference, started empty for
  these recordings and removed after. An administrator wrote a neutral catalogue that plays
  every role of the server's contracts (tables of both entity types with columns and partition
  keys, a deleted table, a name with a quote, a space and an accent, two glossaries with a term
  of one related to a term of the other, a category, classifications and business metadata);
  every answer was then read by an account of the `DATA_SCIENTIST` role, which may only read.
  `anonymize.py` changed nothing but its GUIDs (sequential), times (fixed) and the build
  revision and version (zeros, `2.1.0`): its names and texts were neutral already.
- `atlas-2.1`: a vendor build of Atlas 2.1, a real catalogue made neutral.
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

`atlas-2.1/glossary-term-not-found.json` is the answer of the target Atlas 2.1 to a glossary
term read by a GUID it does not hold (`ATLAS-404-00-005`), recorded as it came, the GUID being
neutral already. Atlas 2.4 is not recorded for this code.

The 2.1 answers were changed further, by hand:

- business attributes were removed: tables keep `stewardship.approvalStatus` and
  `stewardship.ownerTeam`, columns and terms keep `approvalStatus` only;
- the glossary listing was rewritten: it announces 29 terms, in pages of 10 at offsets 0, 10
  and 20, read as a client pages, and holds every term the other recordings name.

`test_recordings.py` checks that the recordings agree with one another; `anonymize.py` says how
to add a version.
