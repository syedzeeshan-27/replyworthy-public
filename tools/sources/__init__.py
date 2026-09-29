"""Pluggable lead-source connectors (plans/v2/03).

One connector per lead source, all speaking the same contract:
`SourceConnector.search(spec) -> list[RawLead]` (see base.py). The pipeline
stays source-agnostic — tools/parse_query.py's route_source() picks which
connector a LeadSpec runs on, in code, never the model.
"""
