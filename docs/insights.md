# Insights

`python -m app insights build` reads three things: the papers in the database with their stored
texts, the clock's day, and the settings. It writes one folder: `insights/` in the home, or the
folder given with `--out`. Every build computes every file and writes it. Two builds of the same
papers, clock day and settings write the same bytes into every file except `runs.jsonl`, on every
supported Python, on any machine and under any `PYTHONHASHSEED`.

Forms used throughout:

- A `.json` file is one JSON value on one line, keys sorted, no spaces, UTF-8 without escapes, then a
  line feed. A `.jsonl` file is one such object per line; a file without rows is empty (0 bytes).
- Floats are written as Python writes them (the shortest text that reads back to the same number);
  counts, ranks, sizes and versions are integers. A row has every key of its file, with `null` or
  `[]` where there is nothing.
- Papers are named by arXiv id without version (`2608.90105`, `cs/9812901`). No database id appears.
- Days are `2026-09-03`, instants `2026-09-03T08:00:00Z`, ISO weeks `2026-W36`, months `2026-09`.
- Logarithms are natural logarithms of whole numbers; `ln` below always means that.

Words used below: a paper is *stored* when it is a row of `papers.jsonl`; *listed* when it is not
withdrawn; a *source* when it is listed and in state `analysed` (its reference list counts). The
*feed set* is the sorted set of all codes in `feeds` of any paper, withdrawn ones included.

## Settings

| Section | Key | Default | Meaning |
|---|---|---|---|
| `text` | `extra_stopwords` | `[]` | words dropped besides the bundled stop words (lower case, `a-z`, digits, `-`) |
| `text` | `min_token_len` | 3 | shortest token kept (1 to 20) |
| `vocab` | `min_df` | 3 | fewest reference papers a term must occur in |
| `vocab` | `max_df_ratio` | 0.4 | largest share of reference papers a term may occur in |
| `vocab` | `min_reference` | 100 | fewest papers before the month for the month to be the reference |
| `similar` | `top_k` | 10 | length of a neighbour list (1 to 100) |
| `similar` | `probe_terms` | 8 | heaviest terms of a list owner used to find candidates |
| `similar` | `min_shared` | 3 | probe terms a candidate must hold |
| `similar` | `min_sim` | 0.12 | lowest score kept in a list |
| `citations` | `damping` | 0.85 | PageRank damping (above 0, below 1) |
| `citations` | `list_size` | 5 | length of the co-citation and coupling lists |
| `topics` | `iterations` | 12 | rounds of the topic fit |
| `topics` | `min_topic_sim` | 0.08 | lowest similarity at which a paper is put with a topic |
| `topics` | `card_terms` | 10 | terms on a topic's card |
| `topics` | `exemplars` | 5 | typical papers on a topic's card |
| `trends` | `recent_weeks` | 4 | weeks of the recent window |
| `trends` | `baseline_weeks` | 8 | weeks of the baseline window |
| `trends` | `top` | 15 | longest list of rising terms, bursts and active authors |
| `ranking` | `novelty` | 0.6 | weight of the novelty part of a score |
| `ranking` | `grounding` | 0.25 | weight of the grounding part |
| `ranking` | `heat` | 0.15 | weight of the heat part |
| `digests` | `top` | 10 | papers shown on a digest page |

Values come in three layers, later ones winning key by key: the defaults of the code,
`insights.yaml` in the project folder, `insights.yaml` in the home. A file may hold any part of the
settings. `python -m app insights config` prints the values in force, in the same YAML form; written
into the home's `insights.yaml` it changes nothing. An unknown section or key, a value of the wrong
type or out of range ends the command with status 2 and the line `error: <file>: <problem>`. YAML
reads unquoted `no`, `yes`, `on`, `off`, `true`, `false` and `null` as something other than words;
write them in quotes in `extra_stopwords`. The settings hash in `stats.json` and `runs.jsonl` is the
SHA-256 of the settings as canonical JSON.

## Day, week and month

The as-of day is the clock's UTC date when the build began. Weeks are ISO weeks, Monday 00:00:00Z
to Sunday 23:59:59Z, labelled with the ISO year and number of their Monday (`2026-W36`), so labels
sort like the weeks (`2026-W53` before `2027-W01`). The as-of week is the week of the as-of day; a
week is closed when its Monday is earlier than the Monday of the as-of week. The as-of month is the
month of the as-of day.

- The day shows in `stats.json` (`as_of.day`) and in the third line of every digest page whose week
  is not closed (`open, day <n> of 7`).
- The week shows in `stats.json` (`as_of.week`); in the trends files, which count closed weeks
  only; in `recent` of a topic (the as-of week and the three before it); and in whether a digest
  page says `closed`.
- The month shows in `stats.json` (`as_of.month`) and in `vocab.json` (`reference.month`): the
  reference papers are those first stored before the month began.
- A ranking uses no as-of value: each bucket uses the last day of its own week.

Moving the clock within a day changes no file but `runs.jsonl`.

## Files

### `papers.jsonl`

One row per stored paper, ascending `arxiv_id`.

| Key | Type | Value |
|---|---|---|
| `arxiv_id` | string | the id without version |
| `version` | integer | the current version |
| `title`, `abstract` | string | of the current version, as stored |
| `authors` | list of strings | of the current version, in paper order |
| `categories` | list of strings | arXiv's own categories of the current version |
| `feeds` | list of strings | the subscribed feeds that list the paper, ascending |
| `published` | instant | first submission |
| `updated` | instant or null | the API's `updated` of the current version |
| `announced` | day | the UTC day the paper was first stored |
| `state` | string | `analysed`, `failed` or `pending` (below) |
| `withdrawn` | boolean | the tracker has seen the paper withdrawn |
| `keywords` | list of strings or null | the analysis keywords, when `state` is `analysed` |
| `novelty` | integer or null | the novelty score, when `state` is `analysed` |
| `text_sha256`, `text_chars` | string, integer or null | hash and length of the stored text, when `state` is `analysed` and the text is of the current version |
| `first_title`, `first_abstract` | string | title and abstract of the first stored version |

`state` is `analysed` when an analysis of the current version exists; else `failed` when the last
try left an error; else `pending`. A paper being worked on keeps its state. An analysis of an older
version gives nothing to the row.

### `text/tokens.jsonl`

One row per stored paper, ascending `arxiv_id`: `arxiv_id`; `terms` and `first_terms`, each an object
term to count. `terms` counts over the current title, the abstract and each analysis keyword as a
block of its own; `first_terms` over `first_title` and `first_abstract`. Body text gives no terms.

A block is normalised in four steps: a `-` and line end between two lower-case letters before and
one after is removed (`embed-` + `dings` gives `embeddings`); every span from a `$` to the next `$`
becomes a space; NFKD normalisation, then combining marks (U+0300 to U+036F) are dropped; lower case.
Then every character other than `a-z`, `0-9` and `-` separates tokens. A sentence ends at `.`, `!`,
`?`, `;` or `:` followed by a space, a line end or the end of the block. A token is a letter followed
by letters and digits, with single inner hyphens (`graph-based`, `word2vec`). A token is kept when it
has at least `min_token_len` characters and is not a stop word (the 163 words of
`app/insights/stopwords.txt` and `extra_stopwords`), both tested before stemming. A kept token is
stemmed after its last hyphen, by the first rule that applies:

| The part ends with | When | Result |
|---|---|---|
| (it is `series` or `species`) | always | unchanged |
| `sses` | 2 or more characters before | `ss` |
| `ies` | 3 or more before | `y` |
| `ings`, `ing`, `ed` | 4 or more before | removed, then un-doubled |
| `es` | 3 or more before, which end in `s`, `x`, `z`, `ch` or `sh` | removed |
| `s` | 3 or more before, and not ending in `ss`, `us`, `is`, `as` | removed |

Un-doubling removes the last letter when the rest ends with the same letter twice and that letter
is one of `bdgmnprt` (`embedd` gives `embed`). The terms are every kept token and every pair of
neighbouring kept tokens of a sentence, written with one space (`graph embed`); no pair reaches
across a sentence end or from one block into the next.

### `text/refs.jsonl`

One row per paper in state `analysed`, ascending `arxiv_id`.

| Key | Type | Value |
|---|---|---|
| `arxiv_id` | string | the paper |
| `text_sha256` | string or null | SHA-256 of the text that was read; null when there was no text file |
| `entries` | integer | entries of the reference list |
| `cites` | list of strings | arXiv ids the list names, in list order, without repeats and without the paper's own id |
| `other` | integer | entries that name no arXiv id |

The heading is the last line that is `references` or `bibliography` once spaces are stripped and
case is folded. The non-empty lines after it are the list; its first line starts with `[1] ` or
`1. `, otherwise there are no entries. A line that starts with the marker of the next number starts
an entry; any other line continues the current one. An entry's id is the leftmost of the two forms
`YYMM.NNNN` or `YYMM.NNNNN` (optionally followed by `v` and a version) and `archive/YYMMNNN`
(`cs/9812901`, `hep-th/9901901`) whose month is 01 to 12, without its version; at most one id counts
per entry.

### `vocab.json`

```json
{"reference":{"month":"2026-09","papers":5,"whole_library":true},"terms":{"speech":{"df":3,"idf":1.4054651081081644}}}
```

| Key | Type | Value |
|---|---|---|
| `reference.month` | month | the as-of month |
| `reference.papers` | integer | number of reference papers |
| `reference.whole_library` | boolean | true when the reference papers are the whole library |
| `terms` | object | each kept term to `{"df", "idf"}` |

The reference papers are the papers first stored before the as-of month began, each with the
terms of the title and abstract it had when first stored. When fewer than `min_reference`
such papers exist, the reference is every stored paper and `whole_library` is true. Papers count in
any state, withdrawn ones too. `df` is the number of reference papers that hold the term; a term is
kept when `min_df <= df <= max_df_ratio * n`, where `n` is the number of reference papers.
`idf = ln(n + 1) - ln(df + 1) + 1`.

### `vectors.jsonl`

One row per stored paper, ascending `arxiv_id`: `arxiv_id` and `vector`, a list of `[term, weight]`
ascending by term. For each term of the paper's current title, abstract and keywords that is in the
vocabulary, with `count` its count, `x = (1 + ln count) * idf`;
the weight is `x` divided by the length of the vector (the square root of the sum of the squares of
all `x`). A paper without a vocabulary term has `[]`.

### `similar.jsonl`

One row per listed paper, ascending `arxiv_id`: `arxiv_id` and `similar`, a list of
`{"id", "score"}`.

The score of two papers is the dot product of their vectors, added over the shared terms in
ascending term order; it is the same number from either side. The list of an owner is searched
this way: its probe terms are its `probe_terms` heaviest terms (by weight, then term); the
candidates are the other listed papers whose vectors hold at least `min(min_shared, number of probe
terms)` of them. The test is made from the owner's side, so one paper can stand in another's list
without the reverse. A candidate is kept when its score is at least `min_sim`. The list is the first
`top_k` kept candidates by falling score, smaller id first among equal scores. A withdrawn paper has
no list and is in none; an owner without a vocabulary term has `[]`.

### `citations/edges.jsonl`

One row per edge, ordered by `src`, then `dst`: `src` cites `dst`, both strings. Only sources cite.
There is an edge for every arXiv id a source's reference list names that is a stored paper, in any
state, withdrawn ones included.

### `citations/external.jsonl`

One row per cited id that is not a stored paper, ascending: `arxiv_id`, and `cited_by`, the number
of sources whose reference list names it.

### `citations/nodes.jsonl`

One row per stored paper, ascending `arxiv_id`.

| Key | Type | Value |
|---|---|---|
| `cited_by` | integer | sources that cite the paper |
| `cites` | integer | stored papers the paper cites |
| `external` | integer | ids it cites that are not stored papers |
| `cocited` | list of `{"id", "count"}` | papers cited together with it: for every source that cites the paper, each other paper that source cites counts once; kept with a count of 2 or more, by falling count, then id; the first `list_size` |
| `coupled` | list of `{"id", "shared", "union"}` | for a source, the other sources that share 2 or more of its cited ids, stored or not; `union` is the cited ids of both less `shared`; by falling `shared`, then smaller `union`, then id; the first `list_size`. `[]` for a paper that is not a source |

### `citations/pagerank.jsonl`

One row per stored paper, ascending `arxiv_id`: `arxiv_id`; `component`, the smallest id of its
connected part with the direction of edges ignored (a paper without edges is a part of its own);
`pagerank`, a float.

PageRank starts at `1 / n` for each of the `n` stored papers and runs exactly 40 rounds. In a round,
a paper with outgoing edges gives `rank / (its outgoing edges)` along each of them; the ranks of
papers without outgoing edges are added up and spread evenly, `spread = total / n`; the new rank of
a paper is `(1 - damping) / n + damping * (incoming + spread)`. The ranks add up to 1 within 1e-9.

### `centroids.json`

`k` (number of topics), `fitted` (number of fit papers), and `topics`, a list of `{"topic", "seed",
"members", "centroid"}`: the number from 0, the paper the topic started from, the fit papers in it,
and the centroid as `[term, weight]` pairs ascending by term.

The fit papers are the reference papers whose first stored title and abstract hold at least one
vocabulary term; each is fitted with the vector of those terms. The number of
topics is the whole number nearest to the square root of half the number of reference papers
(`(isqrt(2 * n) + 1) // 2`), kept between 8 and 40, and at most the number of fit papers. The first
seed is the smallest id; each next seed is the fit paper whose nearest seed is farthest from it
(smallest dot product), the smaller id among equals. Then `iterations` rounds, always all of them:
every fit paper joins the centroid with the largest dot product (the lower topic among equals); each
topic with members gets the sum of its members' vectors divided by its length; a topic without
members keeps its centroid.

### `assignments.jsonl`

One row per listed paper, ascending `arxiv_id`: `arxiv_id`; `topic`, the topic whose centroid has the
largest dot product with the paper's vector, when that is at least `min_topic_sim`, else null; `sim`,
that dot product (0.0 for a paper without a vocabulary term).

### `topics.json`

`k` and `topics`, in topic order, each:

| Key | Type | Value |
|---|---|---|
| `topic` | integer | the number |
| `terms` | list of strings | the first `card_terms` terms of the centroid by falling weight, then term |
| `exemplars` | list of `{"arxiv_id", "title"}` | the first `exemplars` fit papers of the topic that are not withdrawn, by falling `sim`, then id, with their titles of today |
| `name`, `blurb` | strings | the model's name and one-sentence description of the topic |
| `source` | string | `model`, or `fallback` when the model gave no usable answer or no endpoint is set |
| `size` | integer | listed papers put with the topic |
| `recent` | integer | those of them announced in the as-of week or the three weeks before |

The prompt is a system message asking for a JSON object with `name` and `blurb`, and the user message
`Terms: <terms joined by ", ">`, `Papers:`, then one line `- <title>` per exemplar. An answer is used
when `name` has 1 to 80 characters and `blurb` at most 300, white space squeezed. A fallback name is
the first three terms joined by ` / ` with an empty blurb. Every build asks once for every topic.

### `trends/<feed>.json`

One file per feed of the feed set.

| Key | Type | Value |
|---|---|---|
| `feed` | string | the code |
| `weeks` | list of week labels | every week from the first to the last counted week |
| `papers`, `analysed` | lists of integers | per week, listed papers of the feed, and those in state `analysed` |
| `rising` | list of `{"term", "recent", "baseline", "z"}` | keywords rising in the recent window |
| `bursts` | list of `{"term", "count", "before"}` | keywords that burst in the last counted week |
| `topics` | list of `{"topic", "counts", "slope"}` | topics with papers in the last six weeks |

A listed paper of the feed counts in the week of its published day, and only when that week is
closed; an analysed one also counts with each of its keyword terms once. A keyword term is the
keyword normalised as above, its tokens joined by one space, without stemming or stop words. The
windows are counted back from the last counted week: `recent` is that week and the
`recent_weeks - 1` before it, `baseline` the `baseline_weeks` before those, `before` the 8 weeks
before the last week, and the six weeks ending with it. A window may reach before the first week;
those weeks count 0.

Rising: for each term, `a` and `b` are its papers in `recent` and `baseline`, `n1` and `n2` the
analysed papers in them. With `log_odds(c, n) = ln(2c + 1) - ln(2(n - c) + 1)` and
`v = 2/(2a + 1) + 2/(2(n1 - a) + 1) + 2/(2b + 1) + 2/(2(n2 - b) + 1)`,
`z = (log_odds(a, n1) - log_odds(b, n2)) / sqrt(v)`. A term is listed when `a >= 3` and `z > 0`, by
falling `z`, then term; the first `top`. Bursts: `count` is the term's papers in the last week,
`before` those in the 8 weeks before; listed when `count >= 3` and `count * 8 > 2 * before`, by
falling count, then fewer before, then term; the first `top`. Topics: `counts` are the topic's papers
in the six weeks, oldest first; `slope` is `[-5*c1 - 3*c2 - c3 + c4 + 3*c5 + 5*c6, 35]`, the slope of
the straight line through the six counts in papers per week, kept as two integers.

### `authors.json`

Over all listed papers, in the order of published instant, then id. An author is the NFC form of a
name. `weeks` is every week from the first to the last week with a paper, the running week too;
`papers` counts the papers per week; `with_new_author` the papers with at least one author no earlier
paper had. `active` lists `{"author", "papers"}` for the authors with at least 2 papers in the last
`recent_weeks` weeks, by falling count, then name; the first `top`.

### `ranking.jsonl`

One row per bucket, ordered by feed, then week: `feed`, `week`, and `papers`, the members in rank
order, each `{"arxiv_id", "rank", "score", "novelty", "grounding", "heat", "percentile"}`.

A bucket is a feed and a week; its members are the sources that have the feed and were announced in
the week. The end of the bucket is the Sunday of the week. For a member, `grounding` is the number of
stored papers it cites that were announced by the end; `heat` is, over those papers, the number of
their citing papers that were announced by the end, added up. The score is

```text
novelty_weight * novelty / 10
  + grounding_weight * min(grounding, 8) / 8
  + heat_weight * min(1, ln(heat + 1) / ln(51))
```

with the weights of the `ranking` settings. Members are ranked by falling score, smaller id first
among equals, from 1. `percentile` is 100 for a single member, else `100 * (m - rank) // (m - 1)`
with `m` members.

### `digests/<feed>/<week>.md`

One page per bucket:

```text
# cs.LG week 2026-W35

2026-08-24 to 2026-08-30, closed, 3 ranked

1. Sparse Routing for Graph Embeddings (2608.90003)
   K. Maren, T. Soval, R. Ilden et al. | score 0.54 | Graph and Embed
```

Line 3 holds the Monday and Sunday of the week, `closed` or `open, day <n> of 7` (the weekday of the
as-of day, Monday 1), and the number of members. Then two lines for each of the first `top` papers:
rank, title and id; the first three authors (with `et al.` when there are more), the score to two
places, and the name of the paper's topic or `-`. Nothing is escaped.

## Work counters

`python -m app insights show work` prints the counts of the last build, one `name=value` per line in
this order; `runs.jsonl` keeps them for every build.

- `snapshot.rows`: counted once for each paper row the snapshot takes from the database.
- `text.read`: counted once for each text file a build asks for.
- `text.parsed`: counted once for each reference list that is cut into entries and searched for ids.
- `text.tokenized`: counted once for each paper whose terms are made.
- `vocab.fits`: counted once for each fit of the vocabulary.
- `vectors.built`: counted once for each paper vector built.
- `similar.searched`: counted once for each neighbour list searched in full.
- `similar.scored`: counted once for each pair of papers scored.
- `citations.resolved`: counted once for each reference list split into stored and external ids.
- `citations.lists`: counted once for each node record made with its two lists.
- `citations.graphs`: counted once for each pass over the whole graph (parts and PageRank).
- `topics.fits`: counted once for each fit of the topics.
- `topics.assigned`: counted once for each paper put with its nearest topic.
- `labels.asked`: counted once for each request sent to the model endpoint.
- `trends.feeds`: counted once for each feed whose trends are computed.
- `trends.authors`: counted once for each computation of the author statistics.
- `ranking.buckets`: counted once for each bucket ranked.
- `digests.pages`: counted once for each digest page rendered.

## `stats.json` and `runs.jsonl`

`stats.json` describes the result and holds nothing of the run that wrote it: no time, duration,
work count, path or version of a program. Its keys:

| Key | Value |
|---|---|
| `as_of` | `day`, `week` and `month` of the build |
| `settings` | the settings hash |
| `inputs` | `papers`; `analysed`, `failed`, `pending` (rows per state); `withdrawn`; `feeds` (the feed set) |
| `stages` | per stage its `version` and its unit counts: `snapshot` papers; `text` papers, texts, entries, arxiv_refs, other_refs; `vocab` papers, terms; `vectors` papers, weights; `similar` lists, entries; `citations` sources, edges, external, nodes, components; `topics` topics, assigned, unassigned; `trends` feeds; `ranking` buckets, ranked; `digests` pages |
| `files` | every file the build wrote, with its `bytes` and `sha256` |

`runs.jsonl` gets one line at the end of every build that finishes, appended and never rewritten:
`kind` (`build`), `started` (the clock's instant), `as_of` (the day), `settings` (the hash),
`seconds` (per stage, and `total`) and `work` (the counters). It is the one file whose bytes differ
between two builds of the same state; `stats.json` does not name it.

## Check, diff and report

`python -m app insights check` prints one line `<kind>\t<path>` for every file that is `changed`
(size or SHA-256 differ from `stats.json`), `missing` (named there, not on disk) or `unknown` (on
disk, not named; `stats.json` and `runs.jsonl` aside), ascending by path, then
`checked <n> files: <c> changed, <m> missing, <u> unknown`.

`python -m app insights diff A B` prints `differ\t<path>` (with the key of the first differing row
or topic when the file is cut into those), `only-a\t<path>` or `only-b\t<path>` per file, at most 20
of them; then `stage <name>: <d> of <n> units differ` for each of the ten stages; then
`<f> files differ`. Rows are keyed by `arxiv_id` (edges by `<src> <dst>`, buckets by
`<feed> <week>`), `topics.json` by `topic <number>`; other files are one unit each.

`python -m app insights report` writes `report.md` with twelve tables:

- R1 Library by feed: papers, states, withdrawals, later versions, cross-lists, references.
- R2 Weekly intake: papers announced and analysed per week and feed, with the mean novelty.
- R3 Vocabulary: the reference month and the most common terms of each feed.
- R4 Rising terms: the rising keywords of every feed.
- R5 Topics: name, source, size, recent papers, momentum, terms and cohesion of each topic.
- R6 Topics by feed: listed papers per topic and feed.
- R7 Most cited: the most cited papers of the library and the most cited ids it does not hold.
- R8 Citation flow: edges from feed to feed, and the pairs most often cited together.
- R9 Neighbour lists: lengths of the lists, best scores, mutual neighbours, cross-feed shares.
- R10 Versions and withdrawals: new versions and withdrawals per week, from the database.
- R11 Digest leaders: the first three of the last four closed weeks, first-time authors per week.
- R12 Last runs: seconds and work counters of the last five builds.

`audit.txt` has one line `<verdict>\t<name>\t<got>\t<want>` per recount, `ok` or `differ`, then the
line `audit: <N> recounts, <K> differ`, which is also what the command prints. `got` is what the
tables read from the folder, `want` the recount. Against the database: `papers`, `papers in <feed>`,
`state analysed`, `state failed`, `state pending`, `withdrawn`, `analyses of the current version`,
`later versions`. Inside the folder: `edges against cited_by`, `edges against cites`,
`topic sizes and papers without topic`, `neighbour lists out of order`, `neighbour lists too long`,
`neighbour lists naming their owner`, `neighbour lists naming a withdrawn paper`, `intake of <feed>`,
`recent count of <term> in <feed>`, `pagerank total`, `buckets out of order`, `file <path>` for
every file `stats.json` names, and `settings`.
