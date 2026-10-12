class ContractError(ValueError):
    pass

class CapabilityGapError(ContractError):
    def __init__(self, requested_fields: set[str], candidates: dict[str, set[str]]) -> None:
        self.requested_fields = set(requested_fields)
        self.candidates = {name: set(fields) for name, fields in candidates.items()}
        missing = {
            source: sorted(self.requested_fields - fields)
            for source, fields in self.candidates.items()
            if self.requested_fields - fields
        }
        super().__init__(f"no complete source; missing by candidate: {missing}")

class PartialDataNotRequestedError(ContractError):
    pass


class IndependentCalendarError(ContractError):
    """The independent trading calendar could not be verified.

    Raised by calendar providers wired into the ingest router. A transport,
    parse or coverage failure must fail the run: degrading to an empty session
    set would silently satisfy the router's ``observed_dates >=
    expected_dates`` comparison and let unverified coverage publish as
    complete.
    """

    status = "upstream_error"
    retryable = True
