# Custom exceptions for Contracts Control module to avoid circular imports.

class InvalidSpreadsheetError(Exception):
    """Raised when the uploaded spreadsheet is invalid or corrupted."""
    pass


class DatasetWarmingError(Exception):
    """Raised when a cold cache query times out while warming up in background."""
    def __init__(self, retry_after: int = 15):
        self.retry_after = retry_after
        super().__init__("Pipeimob dataset is warming up.")
