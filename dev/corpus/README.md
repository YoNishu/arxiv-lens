# Corpus seed 7

Synthetic arXiv announcements for `dev/standin.py` and `python -m app play`. Made with:

    python dev/make_corpus.py --seed 7 --out <DIR> --weeks 12 --start 2026-08-03 --feeds cs.AI,cs.LG,cs.CL,cs.CV,stat.ML --daily 22 --topics 18

| Item | Value |
|---|---|
| Range | 2026-08-03 to 2026-10-25 |
| Feeds | cs.AI, cs.LG, cs.CL, cs.CV, stat.ML |
| Papers | 1458 |
| Versions | 1580 |
| Replacements | 106 |
| Withdrawals | 16 |
| References | broken 2, duplicate 1, external 3463, internal 10251, other 7186, self 1 |

From 2026-08-31 on, no paper is waiting for another try.

## Planted cases

| Case | Ids | Days | What happens |
|---|---|---|---|
| api_late | 2608.90602 | 2026-09-01, 2026-09-08 | listed on the first day of the month, unknown to the API for a week, then cross-listed |
| body_only | 2608.90454 | 2026-08-21, 2026-09-03 | a version 2 with new body and references, same title and abstract |
| burst | 131 in cs.LG | 2026-08-19 | one feed announces more papers than one API batch holds |
| clean_days |  | 2026-09-02, 2026-09-10 | days with new papers only, each in one feed |
| cocite | 2608.90208, 2608.90212, 2608.90217 | 2026-08-13, 2026-08-14, 2026-08-21 | three papers cited together by the same two papers |
| contents_decoy | 2608.90447 | 2026-08-20 | a contents block with a References line before the first section |
| fading_topic | topic 16 |  | a topic that almost stops after the seventh announcement day before the month turn |
| junk | 2608.90451 | 2026-08-20, 2026-10-25 | an analysis with keywords as one string on every day: three tries, then failed |
| late_cited | 2608.90205 | 2026-08-26, 2026-08-27, 2026-08-28, 2026-08-31, 2026-09-04 | cited four times before it is announced as a cross-list |
| late_link | 2608.90520 | 2026-08-26, 2026-09-03 | a stored paper joins a second feed without a new version |
| late_topic | topic 17 |  | a topic that appears only after the month turn |
| nfd_author | 2608.90555, 2608.90598 | 2026-08-28, 2026-08-31 | one author written with a precomposed and with a decomposed accent |
| no_text | 2608.90315 | 2026-08-18 | a PDF without text |
| pdf_heals | 2608.90544 | 2026-08-27, 2026-09-03, 2026-09-04 | the PDF answers 404 for three tries and comes back later |
| prototype | 2608.90274 | 2026-08-18, 2026-09-03 | a paper at the centre of its topic gets a new title |
| sunday_monday | 2608.90492, 2608.90494 | 2026-08-25 | published on both sides of a week boundary |
| tex_title | 2608.90331 | 2026-08-19 | inline TeX in the title |
| twins | 2608.90222, 2608.90214 | 2026-08-14 | two papers with the same title, abstract, keywords and score |
| unicode_title | 2608.90400 | 2026-08-19 | ligatures, accents, quotes, a dash and a Greek letter in the title |
| withdrawn_early | 2608.90488 | 2026-08-24, 2026-08-25 | the analysis fails once, then the paper is withdrawn |
| withdrawn_hub | 2608.90284, 2608.90304, 2608.90307, 2608.90435, 2608.90461, 2608.90471, 2608.90503 | 2026-08-17, 2026-08-18, 2026-08-19, 2026-08-20, 2026-08-21, 2026-08-24, 2026-08-25, 2026-08-27, 2026-09-08 | seven near-duplicates; the middle one, cited three times, is withdrawn |
| zoo | 2608.90279 | 2026-08-17 | every way of writing an arXiv id in one reference list |

## Fault windows

| Id | Kind | First day | Last day |
|---|---|---|---|
| 2608.90451 | junk | 2026-08-20 | 2026-10-25 |
| 2608.90488 | bad | 2026-08-24 | 2026-08-24 |
| 2608.90544 | nopdf | 2026-08-27 | 2026-09-03 |
| 2608.90602 | gone | 2026-09-01 | 2026-09-01 |

## Reference styles

- A, heading `References`: `[1] L. Sirketzond, K. Vaburrouth, and B. Loleithhu. On depth optical and framewise cameras. arXiv:1606.93628, 2016.`
- B, heading `REFERENCES`: `1. T. Noumseirruth (2026). Ablation calibration with evidence. Preprint, arXiv:2603.99937 [physics.data-an].`
- C, heading `Bibliography`: `[1] V. Fomnaith and S. Drolnet, "Flow-based latent with proximal," Journal of Northern Statistics, vol. 52, no. 9, 2015. DOI 10.9789/documented.231.`
