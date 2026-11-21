"""Terms of titles, abstracts and keywords, and the arXiv ids of reference lists."""
import hashlib
from pathlib import Path

from app.insights import textproc, work

STOPWORDS = """
a about above across after again against all almost also although among an and any are around as at
be because been before being below between both but by can cannot could did do does doing done down
due during each either etc even every few for from further had has have having he hence her here
hers him his how however i if in into is it its itself just may me might more moreover most much
must my neither no nor not now of off often on once one only onto or other our ours out over own
per rather same several shall she should since so some such than that the their theirs them then
there thereby therefore these they this those though through thus to too toward towards two under
unless until up upon us via was we were what when where whereas whether which while who whom whose
why will with within without would yet you your yours
""".split()

STEMS = """
embed embed embeds embed embedded embed embedding embed embeddings embed network network networks network
class class classes class query query queries query study study studies study studied studi
approach approach approaches approach loss loss losses loss box box boxes box bias bias biases bias
feature feature features feature process process processes process analysis analysis analyses analys
train train trains train trained train training train learn learn learned learn learning learn
model model models model modeling model modelling modell run run running run map map mapping map
drop drop dropping drop set set settings set miss miss missing miss add add added added tune tune
tuned tuned tuning tuning string string using using based based series series species species
focus focus corpus corpus gas gas status status axis axis axes axe ones one bayes baye ties tie
node node nodes node index index indices indice propose propose proposes propos proposed propos
generate generate generates generate generated generat generating generat optimization optimization
optimizing optimiz transformer transformer transformers transformer attention attention kernels kernel
less less graph-based graph-based multi-layered multi-layer pre-trained pre-train fine-tuning fine-tuning
class-wise class-wise cross-lingual cross-lingual state-of-the-art state-of-the-art word2vec word2vec
gpt4s gpt4 rnns rnn
""".split()

TITLE = "Sparse Routing for Graph Embeddings"
ABSTRACT = (
    "We study routing in graph embeddings. Sparse gates route each node to a few\n"
    "experts, e.g. by degree. The routed model improves accuracy by 3.2 points and\n"
    "is 0.87 times as costly; training embed-\n"
    "dings takes two hours."
)
KEYWORDS = ["Sparse routing", "graph embeddings", "Mixture of experts"]

FIRST_TERMS = {
    "accuracy": 1, "accuracy point": 1, "costly": 1, "degree": 1, "embed": 3, "embed take": 1, "expert": 1,
    "gate": 1, "gate route": 1, "graph": 2, "graph embed": 2, "hour": 1, "improve": 1, "improve accuracy": 1,
    "model": 1, "model improve": 1, "node": 1, "node expert": 1, "point": 1, "point time": 1, "rout": 3,
    "rout graph": 2, "rout model": 1, "route": 1, "route node": 1, "sparse": 2, "sparse gate": 1,
    "sparse rout": 1, "study": 1, "study rout": 1, "take": 1, "take hour": 1, "time": 1, "time costly": 1,
    "train": 1, "train embed": 1,
}
TERMS = {
    "accuracy": 1, "accuracy point": 1, "costly": 1, "degree": 1, "embed": 4, "embed take": 1, "expert": 2,
    "gate": 1, "gate route": 1, "graph": 3, "graph embed": 3, "hour": 1, "improve": 1, "improve accuracy": 1,
    "mixture": 1, "mixture expert": 1, "model": 1, "model improve": 1, "node": 1, "node expert": 1,
    "point": 1, "point time": 1, "rout": 4, "rout graph": 2, "rout model": 1, "route": 1, "route node": 1,
    "sparse": 3, "sparse gate": 1, "sparse rout": 2, "study": 1, "study rout": 1, "take": 1, "take hour": 1,
    "time": 1, "time costly": 1, "train": 1, "train embed": 1,
}

EXAMPLE_TEXT = (
    "1 Introduction\n"
    "Routing follows [1, 2].\n"
    "\n"
    "\n"
    "References\n"
    "[1] K. Maren and T. Soval. Gated mixtures. arXiv:2607.90011, 2026.\n"
    "[2] R. Ilden. Routing at scale. In Proceedings of the Northern Workshop on\n"
    "Learning Systems, pages 12-34, 2025.\n"
    "[3] A. Dorun et al. Learned routers. arXiv preprint arXiv:2203.91234v2, 2022.\n"
    "[4] K. Maren. Gates again. CoRR, abs/2607.90011, 2026.\n"
)

OWN = "2608.90150"
X1, X2, X3, X4 = "2607.90011", "2607.90023", "2608.90031", "2608.90044"
E1, E2 = "2203.91234", "2204.95678"
ZOO = [
    "K. Maren and T. Soval. Gated mixtures of routed experts. arXiv preprint arXiv:%sv2, 2026." % X1,
    "K. Maren. Gated mixtures again. arXiv:%s, 2026." % X1,
    "R. Ilden, T. Soval, and A. Dorun. Routing at scale for sparse models. https://arxiv.org/abs/%s" % X2,
    "A. Dorun et al. Learned routers for sparse models. http://arxiv.org/pdf/%sv1" % E1,
    "T. Soval. Early sparse coding. arXiv:cs/9812901, 1998.",
    "R. Ilden and K. Maren. Graph limits in practice. https://arxiv.org/abs/math/0001901v2",
    "K. Maren. Kernel routing on graphs. arXiv:0911.9412, 2009.",
    "K. Maren and T. Soval. Sparse routes for graph embeddings. arXiv:%s, 2026." % OWN,
    "T. Soval. Routing notes for the northern group. Technical report (arXiv:%s)." % X3,
    "A. Dorun. Mixtures in practice. CoRR, abs/%s, 2026." % X4,
    "R. Ilden. Broken identifiers in reference lists. arXiv:2608,90777, 2026.",
    "R. Ilden. Spaced identifiers in reference lists. arXiv: 2608. 90778, 2026.",
    "K. Maren. Sparse graphs. Journal of Northern Learning, 12(3):1234-5678, 2021. doi:10.9412/routing.214",
    "T. Soval. Routed experts. In Proceedings of the Northern Workshop on Learning Systems, pages 12-34, 2025.",
]
ZOO_CITES = [X1, X2, E1, "cs/9812901", "math/0001901", "0911.9412", X3, X4]


def _wrap(entry: str, width: int = 60) -> list[str]:
    """Wrap at spaces the way the corpus does: no continuation line starts with a bracket or a digit."""
    lines: list[list[str]] = [[]]
    for word in entry.split(" "):
        if lines[-1] and len(" ".join(lines[-1] + [word])) > width:
            lines.append([])
        lines[-1].append(word)
    for i in range(1, len(lines)):
        while lines[i][0][0] in "[0123456789" and len(lines[i - 1]) > 1:
            lines[i].insert(0, lines[i - 1].pop())
    return [" ".join(words) for words in lines]


def _text(heading: str, entries: list[str], marker: str = "[%d] ", body: str = "", gap: str = "") -> str:
    lines = ["1 Introduction"]
    if body:
        lines.append(body)
    lines.append(heading)
    for n, entry in enumerate(entries, 1):
        if n > 1 and gap:
            lines.append(gap)
        lines.extend(_wrap(marker % n + entry))
    return "\n".join(lines) + "\n"


def _paper(**changes) -> dict:
    paper = {
        "arxiv_id": "2608.90105", "version": 2, "state": "analysed", "title": TITLE, "abstract": ABSTRACT,
        "keywords": KEYWORDS, "first_title": TITLE, "first_abstract": ABSTRACT,
    }
    paper.update(changes)
    return paper


def test_normalise_folds_ligatures_accents_and_case():
    rows = [
        ("ligature", "\ufb01eld \ufb02ow", "field flow"),
        ("precomposed accent", "K\u00f6rnel", "kornel"),
        ("decomposed accent", "nai\u0308ve", "naive"),
        ("accent in a name", "Mar\u00e9n and Mare\u0301n", "maren and maren"),
        ("upper case", "DEEP Graph NETS", "deep graph nets"),
    ]
    for name, text, expected in rows:
        assert textproc.normalise(text) == expected, name
    folded = textproc.normalise("\u201cRouting\u201d with \u03b2-Gates\u2013and Nodes")
    assert textproc.TOKEN.findall(folded) == ["routing", "with", "gates", "and", "nodes"]
    stopwords = textproc.load_stopwords()
    title = "K\u00f6rnel \ufb01elds for \u201cnai\u0308ve\u201d \u03b2-Routing\u2013Graphs"
    for kept in textproc.sentences(title, stopwords, 3):
        for token in kept:
            assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in token), token
    assert textproc.sentences(title, stopwords, 3) == [["kornel", "field", "naive", "rout", "graph"]]


def test_tex_spans_are_dropped():
    assert textproc.TOKEN.findall(textproc.normalise("$\\alpha$-Stable Processes")) == ["stable", "processes"]
    assert textproc.sentences("$\\alpha$-Stable Processes", frozenset(), 3) == [["stable", "process"]]
    assert textproc.TOKEN.findall(textproc.normalise("Sorting in $O(n \\log n)$ time")) == ["sorting", "in", "time"]
    assert textproc.TOKEN.findall(textproc.normalise("$k$-Nearest Graphs")) == ["nearest", "graphs"]
    # A dollar sign without a partner stays and separates like any other character.
    assert textproc.normalise("price$value") == "price$value"
    assert textproc.TOKEN.findall(textproc.normalise("price$value")) == ["price", "value"]
    assert textproc.TOKEN.findall(textproc.normalise("one $two$ three $four")) == ["one", "three", "four"]


def test_words_split_at_a_line_end_are_joined():
    rows = [
        ("classi-\nfication", "classification"),
        ("training embed-\ndings takes", "training embeddings takes"),
        ("Multi-\nHead", "multi-\nhead"),
        ("a-\nb", "a-\nb"),
        ("graph-\n3d", "graph-\n3d"),
    ]
    for text, expected in rows:
        assert textproc.normalise(text) == expected, text
    assert textproc.TOKEN.findall(textproc.normalise("Multi-\nHead")) == ["multi", "head"]


def test_sentences_end_at_punctuation_before_a_space():
    stopwords = textproc.load_stopwords()
    rows = [
        ("every mark", "graph alpha. node beta! edge gamma? path delta; loop sigma: tree omega",
         [["graph", "alpha"], ["node", "beta"], ["edge", "gamma"], ["path", "delta"], ["loop", "sigma"],
          ["tree", "omega"]]),
        ("a line end", "graph alpha.\nnode beta", [["graph", "alpha"], ["node", "beta"]]),
        ("the end of the block", "graph alpha.", [["graph", "alpha"]]),
        ("a decimal number", "score 3.2 points", [["score", "point"]]),
        ("a mark before a letter", "ratio:value graph", [["ratio", "value", "graph"]]),
        ("after e.g.", "methods e.g. kernels", [["method"], ["kernel"]]),
        ("a sentence without a kept token", "graph alpha. it is. node beta", [["graph", "alpha"], ["node", "beta"]]),
    ]
    for name, text, expected in rows:
        assert textproc.sentences(text, stopwords, 3) == expected, name
    assert textproc.SENTENCE_END.split("a b. c d") == ["a b", "c d"]


def test_token_boundaries():
    text = "graph-based state-of-the-art word2vec 3d x-ray trailing- don't a--b"
    assert textproc.TOKEN.findall(textproc.normalise(text)) == [
        "graph-based", "state-of-the-art", "word2vec", "x-ray", "trailing", "don", "t", "a", "b",
    ]
    assert textproc.TOKEN.findall("3d 12ab") == []
    assert textproc.TOKEN.findall("-graph") == ["graph"]


def test_short_tokens_and_stop_words_are_dropped_before_stemming():
    stopwords = textproc.load_stopwords()
    rows = [
        ("short tokens", "the big ox runs", stopwords, 3, [["big", "run"]]),
        ("every length", "the big ox runs", stopwords, 1, [["big", "ox", "run"]]),
        ("an extra stop word", "the big ox runs", stopwords | frozenset(["big"]), 3, [["run"]]),
        ("a stop word is tested before stemming", "ones", stopwords, 3, [["one"]]),
        ("the length is tested before stemming", "nodes node", stopwords, 5, [["node"]]),
        ("only stop words", "the and of", stopwords, 1, []),
    ]
    for name, text, words, min_len, expected in rows:
        assert textproc.sentences(text, words, min_len) == expected, name
    assert "one" in stopwords


def test_stems_follow_the_table():
    assert len(STEMS) % 2 == 0
    pairs = list(zip(STEMS[0::2], STEMS[1::2]))
    assert len(pairs) == 96
    for token, expected in pairs:
        assert textproc.stem(token) == expected, token


def test_only_the_last_part_of_a_hyphenated_token_is_stemmed():
    rows = [
        ("multi-layered", "multi-layer"),
        ("pre-trained", "pre-train"),
        ("fine-tuning", "fine-tuning"),
        ("graph-based", "graph-based"),
        ("class-wise", "class-wise"),
        ("layers-trained", "layers-train"),
        ("nodes-graphs", "nodes-graph"),
    ]
    for token, expected in rows:
        assert textproc.stem(token) == expected, token


def test_pairs_are_neighbours_within_a_sentence():
    stopwords = textproc.load_stopwords()
    assert textproc.count_terms(["routing of sparse graphs"], stopwords, 3) == {
        "graph": 1, "rout": 1, "rout sparse": 1, "sparse": 1, "sparse graph": 1,
    }
    across_sentence = textproc.count_terms(["sparse graphs. dense nodes"], stopwords, 3)
    assert "graph dense" not in across_sentence
    assert across_sentence["sparse graph"] == 1 and across_sentence["dense node"] == 1
    across_blocks = textproc.count_terms(["sparse graphs", "dense nodes"], stopwords, 3)
    assert "graph dense" not in across_blocks
    assert across_blocks == across_sentence
    assert textproc.count_terms(["graph graph graph"], stopwords, 3) == {"graph": 3, "graph graph": 2}
    assert list(textproc.count_terms(["zeta alpha beta"], stopwords, 3)) == [
        "alpha", "alpha beta", "beta", "zeta", "zeta alpha",
    ]


def test_terms_of_the_example_paper():
    work.reset()
    stopwords = textproc.load_stopwords()
    assert textproc.sentences(TITLE, stopwords, 3) == [["sparse", "rout", "graph", "embed"]]
    assert textproc.sentences(ABSTRACT, stopwords, 3) == [
        ["study", "rout", "graph", "embed"],
        ["sparse", "gate", "route", "node", "expert"],
        ["degree"],
        ["rout", "model", "improve", "accuracy", "point", "time", "costly"],
        ["train", "embed", "take", "hour"],
    ]
    row = textproc.tokens_row(_paper(), stopwords, 3)
    assert row == {"arxiv_id": "2608.90105", "terms": TERMS, "first_terms": FIRST_TERMS}
    assert len(row["first_terms"]) == 36 and len(row["terms"]) == 38
    assert work.counts()["text.tokenized"] == 1


def test_keywords_are_blocks_of_their_own():
    stopwords = textproc.load_stopwords()
    paper = _paper(title="Sparse routes", abstract="We study dense nodes", keywords=["graph models", "kernel"],
                   first_title="Sparse routes", first_abstract="We study dense nodes")
    row = textproc.tokens_row(paper, stopwords, 3)
    assert "node graph" not in row["terms"]
    assert "model kernel" not in row["terms"]
    assert row["terms"]["graph model"] == 1 and row["terms"]["kernel"] == 1
    assert "graph" not in row["first_terms"]
    plain = textproc.tokens_row(_paper(keywords=None), stopwords, 3)
    assert plain["terms"] == textproc.count_terms([TITLE, ABSTRACT], stopwords, 3)
    assert plain["terms"] == FIRST_TERMS
    empty = textproc.tokens_row(_paper(keywords=[]), stopwords, 3)
    assert empty["terms"] == FIRST_TERMS


def test_stop_word_file_is_sorted_and_complete():
    data = Path(textproc.__file__).with_name("stopwords.txt").read_bytes()
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    lines = data.decode("ascii").split("\n")[:-1]
    assert lines == sorted(lines)
    assert len(lines) == len(set(lines))
    assert all(line == line.lower() and line.strip() == line and line for line in lines)
    assert lines == STOPWORDS
    assert len(lines) == 163
    assert textproc.load_stopwords() == frozenset(STOPWORDS)


def test_reference_list_of_the_example():
    work.reset()
    row = textproc.refs_row(_paper(), EXAMPLE_TEXT)
    assert row == {
        "arxiv_id": "2608.90105", "cites": ["2607.90011", "2203.91234"], "entries": 4, "other": 1,
        "text_sha256": hashlib.sha256(EXAMPLE_TEXT.encode("utf-8")).hexdigest(),
    }
    assert work.counts()["text.parsed"] == 1
    assert textproc.split_entries(EXAMPLE_TEXT)[1] == (
        "R. Ilden. Routing at scale. In Proceedings of the Northern Workshop on Learning Systems, pages 12-34, 2025."
    )


def test_every_way_an_id_is_written():
    rows = [
        ("arXiv:2608.90001", "2608.90001"),
        ("(arXiv:2608.90001)", "2608.90001"),
        ("abs/2608.90001.", "2608.90001"),
        ("https://arxiv.org/abs/2608.90001v12", "2608.90001"),
        ("arXiv e-prints, 0911.9412, 2009.", "0911.9412"),
        ("arXiv:cs/9812901, 1998.", "cs/9812901"),
        ("https://arxiv.org/abs/math/0001901v2", "math/0001901"),
        ("arXiv:hep-th/9901901v1, 1999.", "hep-th/9901901"),
        ("2613.90001", None),
        ("cs/9913901", None),
        ("arXiv:2608,90001", None),
        ("arXiv: 2608. 90001", None),
        ("12608.90001", None),
        ("2608.900011", None),
        ("pages 1234-5678, 2020.", None),
        ("12(3):1234-5678", None),
        ("doi:10.9412/routing.214", None),
        ("math.AG/0601901", None),
        ("see 2613.90001 and then 2608.90002", "2608.90002"),
        ("cs/9812901 before 2608.90001", "cs/9812901"),
        ("2608.90001 before cs/9812901", "2608.90001"),
        ("cs/9913901 then math/0001901", "math/0001901"),
    ]
    for text, expected in rows:
        assert textproc.find_id(text) == expected, text


def test_the_fourteen_entries_of_the_zoo_list():
    work.reset()
    text = _text("References", ZOO, body="This extends the analysis of arXiv:%s to the routed setting." % E2)
    wrapped = [n for n, entry in enumerate(ZOO, 1) if len(_wrap("[%d] %s" % (n, entry))) > 1]
    assert len(wrapped) >= 10
    entries = textproc.split_entries(text)
    assert entries == ZOO
    found = textproc.references(text, OWN)
    assert found == {"entries": 14, "cites": ZOO_CITES, "other": 4}
    assert E2 not in found["cites"]
    assert work.counts()["text.parsed"] == 1


def test_dot_style_and_the_third_style():
    style_b = [
        "K. Maren and T. Soval (2024). Gated mixtures of routed experts. arXiv:2607.90011.",
        "R. Ilden (2023). Routing at scale. Northern Workshop on Learning Systems.",
        "A. Dorun et al. (2022). Learned routers for sparse models. https://arxiv.org/abs/2203.91234v3",
        "T. Soval (2021). Sparse graphs. Journal of Northern Learning 12, 1234-5678. https://doi.org/10.9412/routing.214",
        "K. Maren (2020). Routed experts in practice. Preprint, arXiv:2005.90321 [cs.LG].",
    ]
    style_c = [
        'K. Maren and T. Soval, "Gated mixtures of routed experts," arXiv e-prints, 2607.90011, 2024.',
        'R. Ilden, "Routing at scale," in Northern Workshop on Learning Systems, 2023, pp. 12-34.',
        'A. Dorun et al., "Learned routers for sparse models," arXiv:2203.91234v2 (2022).',
        'T. Soval, "Sparse graphs," Journal of Northern Learning, vol. 12, no. 3, 2021. DOI 10.9412/routing.214.',
        'K. Maren, "Routed experts in practice," 2020. http://arxiv.org/pdf/2005.90321v1',
    ]
    expected = {"entries": 5, "cites": ["2607.90011", "2203.91234", "2005.90321"], "other": 2}
    rows = [
        ("style B", _text("REFERENCES", style_b, marker="%d. "), style_b),
        ("style C", _text("Bibliography", style_c), style_c),
        ("style A under a heading with spaces", _text("  References ", style_c), style_c),
    ]
    for name, text, entries in rows:
        assert textproc.split_entries(text) == entries, name
        assert textproc.references(text, OWN) == expected, name


def test_the_last_heading_counts():
    entries = ["K. Maren. Gated mixtures. arXiv:2607.90011, 2026.", "R. Ilden. Routing at scale. arXiv:2607.90023, 2025."]
    decoy = "Contents\n1 Introduction\n2 Method\n3 Results\n4 Conclusion\nReferences\n"
    text = decoy + _text("References", entries)
    assert textproc.references(text, OWN) == {"entries": 2, "cites": ["2607.90011", "2607.90023"], "other": 0}
    # Under the first heading the list would start with a section heading and hold no entry.
    assert textproc.split_entries(decoy + "1 Introduction\nSome text.\n") == []


def test_a_page_break_inside_the_list_changes_nothing():
    plain = _text("References", ZOO)
    broken = _text("References", ZOO, gap="\n")
    assert "\n\n\n" in broken
    assert textproc.split_entries(broken) == textproc.split_entries(plain) == ZOO
    assert textproc.references(broken, OWN) == textproc.references(plain, OWN)


def test_own_id_and_repeats_are_dropped():
    entries = [
        "K. Maren. Our earlier note. arXiv:%s, 2026." % OWN,
        "R. Ilden. Gated mixtures. arXiv:%s, 2026." % X1,
        "T. Soval. Routing at scale. arXiv:%s, 2026." % X2,
        "R. Ilden. Gated mixtures, revised. arXiv preprint arXiv:%sv3, 2026." % X1,
        "K. Maren. Our earlier note again. https://arxiv.org/abs/%sv1" % OWN,
    ]
    found = textproc.references(_text("References", entries), OWN)
    assert found == {"entries": 5, "cites": [X1, X2], "other": 0}


def test_a_number_out_of_sequence_joins_the_entry_before():
    text = (
        "References\n"
        "[1] K. Maren. Gated mixtures. arXiv:2607.90011, 2026.\n"
        "[2] R. Ilden. Routing at scale. arXiv:2607.90023, 2025.\n"
        "[4] T. Soval. Sparse graphs. arXiv:2607.90035, 2024.\n"
    )
    entries = textproc.split_entries(text)
    assert entries == [
        "K. Maren. Gated mixtures. arXiv:2607.90011, 2026.",
        "R. Ilden. Routing at scale. arXiv:2607.90023, 2025. [4] T. Soval. Sparse graphs. arXiv:2607.90035, 2024.",
    ]
    assert textproc.references(text, OWN) == {"entries": 2, "cites": ["2607.90011", "2607.90023"], "other": 0}


def test_a_text_without_a_list_has_no_entries():
    placeholder = "[Text extraction failed \u2014 abstract only]\n\n" + ABSTRACT
    rows = [
        ("no heading", "1 Introduction\nRouting follows [1].\n[1] K. Maren. Gates. arXiv:2607.90011, 2026.\n"),
        ("an unnumbered list", "References\nK. Maren. Gates. arXiv:2607.90011, 2026.\nR. Ilden. Routes. 2025.\n"),
        ("a list that starts at two", "References\n[2] K. Maren. Gates. arXiv:2607.90011, 2026.\n"),
        ("a heading and nothing after it", "Some text.\nReferences\n\n\n"),
        ("the placeholder of a text that could not be extracted", placeholder),
        ("an empty text", ""),
    ]
    for name, text in rows:
        assert textproc.split_entries(text) == [], name
        assert textproc.references(text, OWN) == {"entries": 0, "cites": [], "other": 0}, name


def test_a_text_that_cannot_be_read_gives_an_empty_row():
    work.reset()
    row = textproc.refs_row(_paper(), None)
    assert row == {"arxiv_id": "2608.90105", "text_sha256": None, "entries": 0, "cites": [], "other": 0}
    assert work.counts()["text.parsed"] == 0
    row = textproc.refs_row(_paper(), "")
    assert row["text_sha256"] == hashlib.sha256(b"").hexdigest() and row["entries"] == 0
    assert work.counts()["text.parsed"] == 1
