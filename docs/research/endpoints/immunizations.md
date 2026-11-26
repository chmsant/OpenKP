
## Implementation

`list_immunizations` (`openkp/src/openkp/scrapers/immunizations.py`) is built against the shape above. It returns the raw `FormattedAdministeredDates` strings untouched, plus `administered_dates_iso` as a best-effort parse (M/D/YYYY first, matching other legacy endpoints) that is null when the format isn't recognized. Test fixtures use fabricated values and say so. Confirm the date format from one real response before relying on the ISO field.
