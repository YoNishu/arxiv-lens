# Corpus seed 3

Synthetic arXiv announcements for `dev/standin.py` and `python -m app play`. Made with:

    python dev/make_corpus.py --seed 3 --out <DIR> --weeks 5 --start 2026-08-10 --feeds cs.AI,cs.LG,cs.CL --daily 9 --topics 8

| Item | Value |
|---|---|
| Range | 2026-08-10 to 2026-09-13 |
| Feeds | cs.AI, cs.LG, cs.CL |
| Papers | 231 |
| Versions | 241 |
| Replacements | 5 |
| Withdrawals | 5 |
| References | broken 2, duplicate 1, external 568, internal 1424, other 1017, self 1 |

From 2026-08-31 on, no paper is waiting for another try.

## Planted cases

| Case | Ids | Days | What happens |
|---|---|---|---|
| api_late | 2608.90154 | 2026-09-01, 2026-09-08 | listed on the first day of the month, unknown to the API for a week, then cross-listed |
| body_only | 2608.90079 | 2026-08-21, 2026-09-03 | a version 2 with new body and references, same title and abstract |
| clean_days |  | 2026-09-02, 2026-09-10 | days with new papers only, each in one feed |
| cocite | 2608.90033, 2608.90034, 2608.90030 | 2026-08-13, 2026-08-14, 2026-08-21 | three papers cited together by the same two papers |
| contents_decoy | 2608.90085 | 2026-08-20 | a contents block with a References line before the first section |
| fading_topic | topic 6 |  | a topic that almost stops after the seventh announcement day before the month turn |
| junk | 2608.90076 | 2026-08-20, 2026-09-13 | an analysis with keywords as one string on every day: three tries, then failed |
| late_cited | 2608.90028 | 2026-08-26, 2026-08-27, 2026-08-28, 2026-08-31, 2026-09-04 | cited four times before it is announced as a cross-list |
| late_link | 2608.90100 | 2026-08-26, 2026-09-03 | a stored paper joins a second feed without a new version |
| late_topic | topic 7 |  | a topic that appears only after the month turn |
| nfd_author | 2608.90136, 2608.90142 | 2026-08-28, 2026-08-31 | one author written with a precomposed and with a decomposed accent |
| no_text | 2608.90049 | 2026-08-18 | a PDF without text |
| pdf_heals | 2608.90123 | 2026-08-27, 2026-09-03, 2026-09-04 | the PDF answers 404 for three tries and comes back later |
| prototype | 2608.90056 | 2026-08-18, 2026-09-03 | a paper at the centre of its topic gets a new title |
| sunday_monday | 2608.90112, 2608.90113 | 2026-08-25 | published on both sides of a week boundary |
| tex_title | 2608.90062 | 2026-08-19 | inline TeX in the title |
| twins | 2608.90039, 2608.90032 | 2026-08-14 | two papers with the same title, abstract, keywords and score |
| unicode_title | 2608.90080 | 2026-08-19 | ligatures, accents, quotes, a dash and a Greek letter in the title |
| withdrawn_early | 2608.90101 | 2026-08-24, 2026-08-25 | the analysis fails once, then the paper is withdrawn |
| withdrawn_hub | 2608.90048, 2608.90063, 2608.90077, 2608.90068, 2608.90089, 2608.90103, 2608.90106 | 2026-08-17, 2026-08-18, 2026-08-19, 2026-08-20, 2026-08-21, 2026-08-24, 2026-08-25, 2026-08-27, 2026-09-08 | seven near-duplicates; the middle one, cited three times, is withdrawn |
| zoo | 2608.90045 | 2026-08-17 | every way of writing an arXiv id in one reference list |

## Fault windows

| Id | Kind | First day | Last day |
|---|---|---|---|
| 2608.90076 | junk | 2026-08-20 | 2026-09-13 |
| 2608.90101 | bad | 2026-08-24 | 2026-08-24 |
| 2608.90123 | nopdf | 2026-08-27 | 2026-09-03 |
| 2608.90154 | gone | 2026-09-01 | 2026-09-01 |

## Reference styles

- A, heading `References`: `[1] G. Goundbot and S. Kruvairdrar. Procedures propagation with heterophily. In Proceedings of the Coastal Meeting on Data Analysis, pages 3498-3537, 2020.`
- B, heading `REFERENCES`: `1. D. Zitbramo and S. Braifredrer (2000). Listener pitch for timbre audio. Preprint, arXiv:math/0005986 [q-bio.QM].`
- C, heading `Bibliography`: `[1] V. Thudu, "Imagery colours for indicates photographs," in Proceedings of the Coastal Meeting on Data Analysis, 2016, pp. 7478-7493.`
