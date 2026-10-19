# Reviewed legal events

The files here record when provisions of published laws came into force, were repealed, suspended or
revived (CPR-02, IDT-03). The retrieval service uses them to say whether a provision was in force on a
date (`currentness`, basis `provision`). A law without reviewed events is assessed from its record
only, and the answer says so (basis `record`).

## Workflow

1. Propose: `python -m operations.events propose --work <record id>` reads the published text and
   suggests events from its commencement and repeal provisions. A proposal counts for nothing.
2. Review: an editor checks each proposal against the official text, fills in `affected_work`, the
   date and its precision (or a condition when the date is to be appointed), `reviewed_by` and
   `reviewed_at`, and adds events the text does not state (a commencement order, a court's
   declaration of invalidity).
3. Publish: `python -m operations.events publish ingestion/events/<file>.json --dry-run`, then
   without `--dry-run`. The file is refused whole if any event names a record that is not public, or
   a provision that no public text of it has.

## Rules

- Scope is `whole`, or `explicit_nodes` with the provisions' locators (`sec_5`, `sec_5__subsec_2`). An
  event on a provision covers the parts inside it. A scope that the text does not enumerate is never
  written as `whole`.
- Precision is `day`, `month`, `year` or `unknown`. A date known only to the month is `month` with the
  first of the month.
- Published events are never edited. To correct one, add an event with a new `id` that `supersedes`
  it. To retract one, publish it again with `"review_state": "withdrawn"`.
- A profile records a jurisdiction's rule for a document class when an instrument is silent on its
  commencement (`silent_commencement`: `assent` or `unknown`), with the law it rests on. Without a
  reviewed profile, nothing is assumed.

## Example

```json
{
  "profiles": [],
  "events": [
    {
      "id": "evt_1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b",
      "type": "commenced",
      "affected_work": "ng-example-act-2023",
      "scope": "whole",
      "date_from": "2023-06-12",
      "precision": "day",
      "evidence": {"url": "https://example.org/act.pdf", "locator": "sec_66", "quote": "This Act shall come into force on …"},
      "review_state": "reviewed",
      "reviewed_by": "Editor's name",
      "reviewed_at": "2026-10-19T09:00:00+00:00"
    }
  ]
}
```
