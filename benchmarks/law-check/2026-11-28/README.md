# Law check citation benchmark, 28 November 2026

Does the live quotation checker give the right verdict? Each case was sent to `check_claim` on `https://open.alvary.ai/agents/mcp` (the code in `retrieval/verify.py`) on 28 November 2026, one at a time.

This measures the checker only. It does not measure whether an AI answer chooses the right law or applies it correctly; that needs review by a lawyer.

## Cases

Built by `build.py` from the published Nigerian full texts (`/site/v1/sources/{id}/text`): up to three random sections from each of 63 laws, random seed 20261128.

- **exact**: 10 to 18 consecutive words (60 to 200 characters) from a section, cited to that section. Right verdict: found.
- **altered**: the same words with one word changed (for example shall to may, and to or, person to citizen) or one word of four or more letters dropped, checked not to occur in that section. Right verdict: not found.
- **wrong_section**: the exact words cited to another section of the same law that does not contain them. Right verdict: not found.
- **law_not_held**: exact words cited to one of ten invented laws, twice each. Right verdict: not in the catalogue.

`run.py` sends each case and writes `results.jsonl`: the receipt's `status` and its `findings.quote` (`exact`, `normalised`, `with_omissions` mean found; `absent` means not found).

## Results

| Case | Checks | Right verdict |
|---|---|---|
| exact | 159 | 159 found (110 exact, 49 after normalising spaces and punctuation) |
| altered | 159 | 159 not found (151 `wording_differs` or `not_in_force` with the words absent, 8 `ambiguous`) |
| wrong_section | 159 | 159 not found (153 absent, 6 `ambiguous`) |
| law_not_held | 20 | 20 `not_in_catalogue` |

No altered, wrongly cited or unheld quotation was marked found (0 of 338). No request failed. 98 exact checks were on laws recorded as not in force; their receipts say so and still report the words as found.

## Limits

- The quotations were cut mechanically; real misquotations can be subtler.
- Quotations with omissions ("…") were not tested.
- 63 laws and 497 checks; the published Nigerian collection is still small.
