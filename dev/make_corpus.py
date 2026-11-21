"""Seeded generator of synthetic arXiv corpora for the stand-in and for `python -m app play`.

    python dev/make_corpus.py --seed N --out DIR [--weeks W] [--start DAY] [--feeds CODES] [--daily N] [--topics T]

A corpus holds several weeks of arXiv as the tracker meets them: feeds per day, what the API knows,
the lines of every PDF, the model's answers and the days a request fails. The same arguments write
the same content on every supported Python; no PDF is rendered here (the stand-in draws the pages).
"""
import argparse
import bisect
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys

MASK = (1 << 64) - 1


class Rng:
    """splitmix64 on a state taken from the seed and the name of the stream."""

    def __init__(self, seed: int, name: str):
        digest = hashlib.sha256(f"{seed}:{name}".encode("utf-8")).digest()
        self.state = int.from_bytes(digest[:8], "big")

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        return (self.next() * n) >> 64

    def chance(self, num: int, den: int) -> bool:
        return self.below(den) < num

    def pick(self, seq):
        return seq[self.below(len(seq))]

    def weighted(self, weights) -> int:
        r = self.below(sum(weights))  # ints
        total = 0
        for i, w in enumerate(weights):
            total += w
            if total > r:
                return i
        raise ValueError("no positive weight")

    def weighted_cum(self, totals: list) -> int:
        # the same draw as weighted() when the running totals are already at hand
        return bisect.bisect_right(totals, self.below(totals[-1]))

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]

    def sample(self, seq, k: int) -> list:
        items = list(seq)
        self.shuffle(items)
        return items[:k]


def running(weights) -> list:
    totals, total = [], 0
    for w in weights:
        total += w
        totals.append(total)
    return totals


# Latent topics: name, two home-feed positions, 25 word families ("|" between families).
TOPICS = [
    ("graph learning", 1, 0,
     "graph graphs|node nodes nodal|edge edges|embed embedding embeddings embedded|"
     "neighbour neighbours neighbourhood|message messages messaging|propagation propagate propagated|"
     "adjacency|spectral|eigenvector eigenvectors|subgraph subgraphs|vertex vertices|"
     "homophily homophilous|heterophily|pooling pooled|readout|aggregation aggregate aggregated aggregator|"
     "walk walks walker|community communities|link links|hypergraph hypergraphs|motif motifs|"
     "oversmoothing|graph-based|coarsening coarsened"),
    ("language modelling", 2, 1,
     "language languages|token tokens tokenizer tokenization|vocabulary|prompt prompts prompting prompted|"
     "decoder decoders decoding|pretraining pretrained|corpus corpora|perplexity|autoregressive|"
     "sentence sentences|word words|context contexts contextual|instruction instructions|"
     "generation generate generates generating|lexical|syntax syntactic syntactically|"
     "semantic semantics semantically|grammar grammatical|paraphrase paraphrases|discourse|"
     "dialogue dialogues|utterance utterances|n-gram n-grams|subword subwords|text-based"),
    ("machine translation", 2, 2,
     "translation translations translate translated|bilingual|multilingual|source-side|target-side|"
     "alignment alignments aligned|parallel|bitext bitexts|low-resource|encoder encoders encoding|"
     "attention attentive|beam beams|reordering reordered|morphology morphological morphologically|"
     "transliteration|interlingua|fluency fluent fluently|adequacy|back-translation|lexicon lexicons|"
     "phrase phrases phrasal|cross-lingual|dictionary dictionaries|postediting post-edited|monolingual"),
    ("speech and audio", 2, 1,
     "speech|audio|acoustic acoustics|speaker speakers speaking|spectrogram spectrograms|"
     "waveform waveforms|phoneme phonemes phonetic|prosody prosodic|transcription transcriptions transcribed|"
     "vocoder vocoders|pitch|timbre|noise noisy denoising|microphone microphones|diarization|"
     "keyword-spotting|music musical|sound sounds|listening listener listeners|voice voices voiced|"
     "intonation|reverberation|frame-level|mel-scale|streaming streamed"),
    ("image recognition", 3, 1,
     "image images imaging|pixel pixels|convolution convolutional convolutions|"
     "classifier classifiers classification|recognition recognize recognized|texture textures|"
     "patch patches|backbone backbones|augmentation augmented augmentations|resolution|colour colours|"
     "filter filters filtering|visual visually|photograph photographs|label-efficient|fine-grained|"
     "category-level|imagery|thumbnail thumbnails|contrast contrastive|invariance invariant|"
     "crop crops cropping|channel channels|kernel kernels|stride strides"),
    ("detection and segmentation", 3, 3,
     "detection detector detectors detect detected|segmentation segment segments segmented|bounding|"
     "box boxes|mask masks masked|instance instances|panoptic|anchor anchors anchored|"
     "region regions regional|keypoint keypoints|localization localize localized|occlusion occluded|"
     "contour contours|boundary boundaries|foreground|pedestrian pedestrians|tracking tracked tracker|"
     "overlap overlapping|heatmap heatmaps|lidar|point-cloud|scene scenes|object objects|part-based|"
     "saliency salient"),
    ("generative models", 1, 3,
     "generative|diffusion|sampler samplers sampling sampled|latent latents|autoencoder autoencoders|"
     "variational|likelihood likelihoods|flow-based|denoiser denoisers|"
     "synthesis synthesize synthesized synthetic|discriminator discriminators|generator generators|"
     "score-based|reconstruction reconstructions reconstructed|mode-collapse|fidelity|"
     "diversity diverse diversified|interpolation interpolate interpolated|inpainting|"
     "style styles stylized|codebook codebooks|quantization quantized|upsampling upsampled|"
     "realism realistic|energy-based"),
    ("reinforcement learning", 1, 0,
     "reinforcement|reward rewards rewarded|policy policies|agent agents agentic|"
     "environment environments|action actions actionable|episode episodes episodic|value-based|"
     "q-learning|exploration explore explored exploring|exploitation|actor-critic|"
     "trajectory trajectories|return returns|discount discounted|off-policy|on-policy|bandit bandits|"
     "regret|transition transitions|simulator simulators simulated|curriculum|intrinsic|"
     "imitation imitate|rollout rollouts"),
    ("planning and search", 0, 0,
     "planning planner planners planned|search searching searched|heuristic heuristics heuristically|"
     "tree trees|goal goals|state-space|horizon horizons|lookahead|branching|pruning pruned prune|"
     "solver solvers|constraint constraints constrained|schedule schedules scheduling|path paths|"
     "route routes routing routed|plan plans|frontier|backtracking|admissible|"
     "expansion expansions expanded|satisfiability|combinatorial|subgoal subgoals|"
     "precondition preconditions|deadline deadlines"),
    ("knowledge and reasoning", 0, 2,
     "knowledge|reasoning reason reasons reasoned|logic logical|deduction deductive|ontology ontologies|"
     "entity entities|relation relations relational|triple triples|commonsense|symbolic|rule rules|"
     "proof proofs|theorem theorems|argument arguments|chain-of-thought|fact facts factual|multi-hop|"
     "abductive|analogy analogies analogical|concept concepts conceptual|taxonomy taxonomies|"
     "premise premises|entailment|explanation explanations explain explained|puzzle puzzles"),
    ("Bayesian inference", 4, 1,
     "inferential|posterior posteriors|prior priors|inference infer inferred|"
     "marginal marginals marginalized|resampling resampled|hierarchical|conjugate|evidence|credible|"
     "uncertainty uncertainties uncertain|calibration calibrated miscalibrated|hyperprior hyperpriors|"
     "stochastic|probabilistic probability probabilistically|belief beliefs|expectation expectations|"
     "predictive|model-averaging|mixture mixtures|nonparametric|approximate approximation approximations|"
     "chain chains|burn-in|dispersion overdispersed"),
    ("optimisation", 4, 1,
     "optimisation optimiser optimisers optimise optimised|gradient gradients subgradient|"
     "convergence converge converges converged|convex convexity|nonconvex|step-size|momentum|"
     "learning-rate|descent|saddle|regularisation regularised regulariser|objective objectives|"
     "loss losses|stationary|iteration iterations iterative iterate|second-order|first-order|curvature|"
     "preconditioning preconditioner|adaptive adaptively|minimiser minimisers minimise minimised|"
     "smoothness smooth|proximal|dual duality|penalty penalties"),
    ("causal inference", 4, 0,
     "causal causality|causation|confounder confounders confounding confounded|"
     "intervention interventions interventional|counterfactual counterfactuals|"
     "treatment treatments treated|outcome outcomes|propensity|instrument instruments instrumental|"
     "identifiability identifiable identification|backdoor|mediator mediation mediated|effect effects|"
     "placebo placebos|observational|do-calculus|structural|estimand estimands|heterogeneous|selection-bias|"
     "transportability|discovery|adjustment adjusted|collider colliders|ignorability"),
    ("time series", 4, 1,
     "series|temporal temporally|forecast forecasts forecasting forecasted|seasonal seasonality|"
     "trend trends trending|lag lags lagged|autoregression|anomaly anomalies anomalous|sensor sensors|"
     "stationarity|periodic periodicity|window windows windowed|recurrent|timestamp timestamps|"
     "sequence sequences sequential|irregular|multivariate|univariate|drift drifting|imputation imputed|"
     "spike spikes|decomposition|volatility|demand|hourly"),
    ("federated and private learning", 1, 4,
     "federated federation|client clients client-side|privacy private privately|differential|"
     "secure securely|encryption encrypted|communication communications communicate|heterogeneity|"
     "personalization personalized|server servers server-side|leakage leak leaks|membership|"
     "clipping clipped|budget budgets|decentralized|on-device|round rounds|participation participants|"
     "gossip|anonymity anonymous|trust trusted untrusted|unlearning|silo silos|consent|cross-silo"),
    ("retrieval and question answering", 2, 0,
     "retrieval retrieve retrieved retriever|question questions|answer answers answering answered|"
     "passage passages|document documents documented|query queries queried|reranking reranker|"
     "index indexing indexed|dense|relevance relevant|open-domain|reader readers|snippet snippets|"
     "corpus-level|recall|precision|hit hits|clarification|conversational|factoid|span spans|"
     "context-aware|memory memories|lookup lookups|archive archives archival"),
    ("video and 3D vision", 3, 3,
     "video videos|frame frames framewise|motion|depth|stereo|voxel voxels|mesh meshes meshing|"
     "rendering render rendered|camera cameras|pose poses posed|optical|geometry geometric geometrically|"
     "view-synthesis|multi-view|clip clips|spatiotemporal|radiance|surface surfaces surfaced|volumetric|"
     "egocentric|shading|occupancy|silhouette silhouettes|keyframe keyframes|viewpoint viewpoints"),
    ("robustness and fairness", 1, 4,
     "robustness robust robustly|adversarial adversary adversaries|perturbation perturbations perturbed|"
     "fairness fair unfair|bias biases biased|attack attacks attacked|defense defenses|certified|"
     "shift shifts|spurious|demographic demographics|worst-case|out-of-domain|group-level|"
     "disparity disparities|equity equitable|protected|audit audits auditing|accountability|"
     "corruption corruptions corrupted|poisoning poisoned|trustworthy|harm harms harmful|"
     "transparency transparent|sensitive"),
]

# General research vocabulary shared by all topics.
COMMON = [family.split(" ") for family in (
    "method methods|result results|propose proposes proposed|evaluate evaluates evaluated evaluation|"
    "baseline baselines|approach approaches|model models modelling modelled|performance|accuracy accurate|"
    "efficient efficiency efficiently|framework frameworks|experiment experiments experimental|"
    "dataset datasets|benchmark benchmarks|task tasks|analysis analyses analyse analysed|"
    "study studies studied|problem problems|solution solutions|improve improves improved improvement|"
    "training train trained|learning learn learned learner|network networks|"
    "algorithm algorithms algorithmic|structure structures structured|representation representations|"
    "feature features|parameter parameters|scale scales scaling scalable|simple simplicity|"
    "strong stronger|large larger|small smaller|sparse sparsity|linear nonlinear|"
    "function functions functional|estimate estimates estimation estimator|error errors|"
    "test tests testing tested|measure measures measured|metric metrics|setting settings|"
    "design designs designed|component components|module modules modular|layer layers|"
    "architecture architectures|input inputs|output outputs|signal signals|system systems|"
    "process processes processing processed|control controls controlled|complexity complex|"
    "theory theoretical|bound bounds bounded|guarantee guarantees guaranteed|empirical empirically|"
    "practical practice|general generalize generalization|transfer transferable transferred|"
    "domain domains|target targets targeted|variable variables|distribution distributions|"
    "statistic statistics statistical|sample samples|cost costs costly|time times|"
    "compute computation computational|resource resources|data|information|quality|coverage|"
    "consistency consistent|stability stable|variance|capacity|effective effectively effectiveness|"
    "novel|existing|recent|large-scale|state-of-the-art|end-to-end|lightweight|trade-off trade-offs|"
    "insight insights|observation observations observe observed|finding findings|"
    "limitation limitations|challenge challenges challenging|gap gaps|benefit benefits|"
    "advantage advantages|strategy strategies|mechanism mechanisms|criterion criteria|"
    "assumption assumptions assume|condition conditions conditional|case cases|example examples|"
    "scenario scenarios|application applications applied|deployment deploy deployed|user users|"
    "human humans|expert experts|annotation annotations annotated|"
    "supervision supervised unsupervised self-supervised|label labels labelled|weak weakly|"
    "joint jointly|shared sharing|global globally|local locally|multi-task|multi-scale|cross-domain|"
    "zero-shot|few-shot|pipeline pipelines|toolkit|protocol protocols|variant variants|"
    "ablation ablations|comparison comparisons compare compared|survey surveys|review reviews|"
    "interpretability interpretable|sensitivity|throughput|latency|speed speedup|hardware|storage|"
    "quantity quantities|factor factors|weight weights weighting weighted|score scores scoring|"
    "matrix matrices|vector vectors|tensor tensors|dimension dimensions dimensional|space spaces|"
    "projection projections|transformation transformations|operator operators|mapping map maps mapped|"
    "similarity similar|distance distances|cluster clusters clustering|partition partitions|"
    "subset subsets|selection select selected|update updates updated|step steps|stage stages|"
    "phase phases|level levels|rate rates|ratio ratios|margin margins|threshold thresholds|gain gains|"
    "drop drops|increase increases increased|reduce reduces reduced reduction|achieve achieves achieved|"
    "outperform outperforms|demonstrate demonstrates demonstrated|show shows shown|suggest suggests|"
    "indicate indicates|reveal reveals|enable enables enabled|allow allows|require requires required|"
    "combine combines combined combination|extend extends extended extension|"
    "introduce introduces introduced|develop develops developed|build builds built|"
    "capture captures captured|handle handles|address addresses addressed|provide provides provided|"
    "obtain obtains obtained|maintain maintains|preserve preserves preserved|support supports supported|"
    "depend depends dependent dependence|vary varies varying|remain remains|hold holds|lead leads|"
    "yield yields|principle principles|property properties|behaviour behaviours|regime regimes|"
    "hypothesis hypotheses|inductive|training-free|plug-in|open-source|real-world|long-range|"
    "short-term|long-term|high-dimensional|low-rank|fine-tuning fine-tuned|parameterized|"
    "modality modalities multimodal|interaction interactions|dynamics dynamic dynamical|"
    "scheme schemes|technique techniques|procedure procedures|formulation formulations formulate|"
    "perspective perspectives|view views|role roles|impact impacts|pattern patterns|regularity|"
    "order orders ordering ordered|rank ranks|group groups grouping grouped|category categories|"
    "class classes|type types|form forms|size sizes|number numbers|amount|fraction fractions|portion|"
    "majority|minority|extent|degree degrees|range ranges|area areas|field fields|source sources|"
    "origin|basis|foundation foundations"
).split("|")]

FUNCTION_WORDS = ["for", "of", "in", "with", "on", "under", "via", "and", "from", "beyond"]
SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to",
               "under", "via", "with", "beyond"}

ONSETS = ["b", "d", "f", "g", "h", "j", "k", "l", "m", "n", "r", "s", "t", "v", "z", "br", "dr", "fr",
          "gr", "kr", "tr", "st", "sh", "th"]
VOWELS = ["a", "e", "i", "o", "u", "a", "e", "o", "ai", "ei", "ou"]
CODAS = ["", "", "", "n", "r", "l", "s", "m", "t", "nd", "rn", "th"]
ACCENTED = {"e": "\u00e9", "o": "\u00f6", "u": "\u00fc"}

# What a PDF holds instead of the few characters a title, an abstract or a name may carry.
FOLD = str.maketrans({"\u00e9": "e", "\u00f6": "o", "\u00fc": "u", "\u0301": "", "\u0308": "",
                      "\ufb01": "fi", "\ufb02": "fl", "\u2013": "-", "\u201c": '"', "\u201d": '"',
                      "\u03b2": "beta"})

OUTSIDE = ["math.OC", "eess.SP", "cs.IR", "cs.RO", "q-bio.QM", "physics.data-an"]
FEED_WEIGHTS = [20, 30, 18, 22, 10]
REF_COUNT_WEIGHTS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 8, 7, 6, 5, 4, 4, 3, 3, 2, 2, 2, 1, 1]  # 4 to 25
TEAM_SIZE_WEIGHTS = [8, 22, 26, 18, 11, 7, 4, 2, 2]
NOVELTY_WEIGHTS = [1, 3, 8, 14, 20, 20, 16, 10, 6, 2]
FAMILY_TOTALS = {25: running([3] * 10 + [2] * 10 + [1] * 5), 20: running([3] * 10 + [2] * 10)}
PAGE_LINES = 56
WIDTH = 80
ABSTRACT_WIDTH = 72
BURST = 131
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WITHDRAWAL = "This paper has been withdrawn by the authors due to an error in the main result."
HEADINGS = (["1 Introduction"], ["2 Method", "2 Approach", "2 Model", "2 Background"],
            ["3 Experiments", "3 Evaluation", "3 Results"],
            ["4 Conclusion", "4 Discussion", "4 Conclusion and Future Work"])
REF_HEADINGS = {"A": "References", "B": "REFERENCES", "C": "Bibliography"}
TEX = ["$\\alpha$-Stable", "$k$-Nearest", "$O(n \\log n)$"]
VENUES = ["Proceedings of the Northern Workshop on Learning Systems",
          "Proceedings of the Lakeside Conference on Inference",
          "Symposium on Applied Graph Methods", "Workshop on Quiet Computation",
          "Proceedings of the Coastal Meeting on Data Analysis", "Annual Colloquium on Structured Learning",
          "Proceedings of the Highland Forum on Reasoning", "Meeting on Signals and Learning"]
JOURNALS = ["Journal of Applied Inference", "Transactions on Learned Systems", "Letters on Quiet Computation",
            "Bulletin of Adaptive Methods", "Journal of Northern Statistics", "Annals of Structured Learning"]
DOI_WORDS = sorted({form for _, _, _, text in TOPICS for family in text.split("|")
                    for form in family.split(" ") if form.isalpha() and 4 <= len(form) <= 10})

ID_PATTERNS = {
    "A": ["arXiv:{id}, {year}.", "arXiv preprint arXiv:{id}v{k}, {year}.", "CoRR, abs/{id}, {year}."],
    "B": ["arXiv:{id}.", "https://arxiv.org/abs/{id}v{k}", "Preprint, arXiv:{id} [{primary}]."],
    "C": ["arXiv e-prints, {id}, {year}.", "arXiv:{id}v{k} ({year}).", "{year}. http://arxiv.org/pdf/{id}v{k}"],
}
OTHER_PATTERNS = {
    "A": ["In {venue}, pages {pages}, {year}.", "{journal}, {vol}({no}):{pages}, {year}. doi:{doi}"],
    "B": ["{venue}.", "{journal} {vol}, {pages}. https://doi.org/{doi}"],
    "C": ["in {venue}, {year}, pp. {pages}.", "{journal}, vol. {vol}, no. {no}, {year}. DOI {doi}."],
}
# Old-style ids are written after "arXiv:" or inside a URL, never bare.
OLD_STYLE_PATTERN = {("A", 2): 0, ("C", 0): 1}

TITLES = ["{w} {w} for {w} {w}", "{w} {w} with {w} {w} and {w}", "Towards {w} {w} in {w} {w}",
          "{w} {w}: {w} {w} for {w} {w}", "On the {w} of {w} {w}", "Learning {w} {w} from {w} {w}",
          "Can {w} {w} Improve {w} {w}?", "A {w} {w} Approach to {w} {w} {w}",
          "{w} and {w} {w} via {w} {w}", "Rethinking {w} {w} for {w} {w} {w}"]
ABSTRACT = {
    "problem": ["{w} {w} is a central problem in {w} {w}.",
                "Many {w} systems depend on {w} {w} to handle {w} {w} at scale.",
                "Recent work on {w} {w} has made {w} {w} far more common.",
                "Learning {w} {w} from {w} {w} remains difficult in practice.",
                "{w} {w} plays a key role in {w} and {w} {w}."],
    "gap": ["However, existing {w} methods ignore {w} {w}, e.g. when the {w} is {w}.",
            "Prior approaches treat {w} and {w} separately; this limits {w} {w}.",
            "Yet little is known about {w} {w} w.r.t. {w} {w}.",
            "Current {w} pipelines scale poorly with the number of {w} {w}."],
    "proposal": ["We propose {w} {w}, a {w} {w} that combines {w} with {w} {w}.",
                 "In this paper we introduce a {w} {w} for {w} {w}.",
                 "We present a simple {w} {w} built around {w} {w}.",
                 "This work develops {w} {w} (i.e. {w} {w} with {w} {w})."],
    "method": ["Our method learns {w} {w} from {w} {w} and reuses the {w} {w} at test time.",
               "The approach maps each {w} to a {w} {w}: the {w} {w} is then updated in {n} steps.",
               "We train the {w} {w} with a {w} {w} and a \"{w}\" {w} term.",
               "Each {w} {w} is scored against {w} {w} before the {w} step."],
    "result": ["Experiments on {n} {w} benchmarks show gains of {d} points over strong {w} baselines.",
               "On {n} {w} tasks, the model improves {w} {w} by {p}% while using {d} times fewer {w}.",
               "The method reaches {p}% {w} {w} on {n} {w} settings, against {p}% for the best {w} {w}.",
               "Across {n} {w} {w}, it lowers the {w} error by {d} points (from {s} to {s})."],
    "conclusion": ["These findings suggest that {w} {w} is a practical route to {w} {w}.",
                   "The results hold across the model's {w} {w} and {w} {w} settings.",
                   "We release the {w} {w} to support further study of {w} {w}.",
                   "Overall, {w} {w} offers a cheap and {w} way to improve {w} {w}."],
}
BODY = ["Work on {w} {w} has long studied how {w} {w} behaves under {w} {w} {r}.",
        "Following {q}, we model the {w} {w} as a {w} {w} over the {w} {w} of each {w}.",
        "We train on {n} {w} {w} and report the mean over {n} runs with {w} {w} held fixed.",
        "Table {n} lists the {w} {w} for each {w} {w}, together with the {w} {w} of every {w}.",
        "The {w} {w} improves by {d} points over the method of {r}, mostly on {w} {w}.",
        "This choice keeps the {w} {w} stable under {w} {w} and avoids a separate {w} {w}.",
        "A {w} {w} is computed for every {w} and passed to the {w} {w} before the {w} step.",
        "In contrast to {r}, our {w} {w} does not need {w} {w} or any {w} {w} at test time.",
        "We set the {w} {w} to {d} and the number of {w} {w} to {n} in all runs.",
        "Figure {n} shows how the {w} {w} changes with {w} {w} when the {w} grows.",
        "The gap to {w} {w} grows when {w} {w} is {w}, as earlier reports on {w} {w} suggest {r}.",
        "Errors mostly come from {w} {w} with few {w} {w}, which the {w} {w} cannot separate.",
        "Both {w} {w} and {w} {w} are needed to reach {p}% {w} on the {w} {w}.",
        "Removing the {w} {w} lowers the score from {s} to {s}, while the {w} {w} stays the same.",
        "Earlier {w} {w} methods {q} rely on {w} {w} and a fixed {w} {w}."]
REF_TITLES = ["{w} {w} for {w} {w}", "{w} of {w} {w}", "on {w} {w} and {w} {w}", "{w} {w} with {w}",
              "{w} {w} {w} {f} {w} {w}", "towards {w} {w} {f} {w}"]
CONTRIBUTIONS = ["A {w} {w} for {w} {w}", "An analysis of {w} {w} under {w} {w}",
                 "Experiments on {n} {w} {w} settings", "A {w} {w} that needs no {w} {w}"]
METHOD_EXTRA = ["Training uses {n} {w} {w} and a {w} {w}.", "The {w} {w} is tuned on held-out {w} {w}."]
LIMITATIONS = ["The study covers only {n} {w} {w} settings.", "The {w} {w} assumes clean {w} {w}.",
               "Results on {w} {w} beyond {n} {w} are not reported."]

SLOT = re.compile(r"\{([a-z])\}")
DAY_TEXT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
FEED_CODE = re.compile(r"^[a-z][a-z-]*(\.[A-Za-z][A-Za-z-]*)?$")


class UsageError(Exception):
    pass


def fold(text: str) -> str:
    return text.translate(FOLD)


def at(day: dt.date, second: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day) + dt.timedelta(seconds=second)


def instant(moment: dt.datetime) -> str:
    return (f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
            f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}Z")


def title_case(word: str, first: bool) -> str:
    if word in SMALL_WORDS and not first:
        return word
    return "-".join(part[:1].upper() + part[1:] for part in word.split("-"))


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


class Words:
    """Where the content words of one paper come from."""

    def __init__(self, topics: list, share=(3, 4), families: int = 25):
        self.topics = topics
        self.share = share
        self.families = families

    def topic_word(self, rng: Rng) -> str:
        topic = self.topics[0] if len(self.topics) == 1 else self.topics[rng.weighted([2, 1])]
        family = FAMILIES[topic][rng.weighted_cum(FAMILY_TOTALS[self.families])]
        return rng.pick(family)

    def content(self, rng: Rng):
        if rng.chance(*self.share):
            return self.topic_word(rng), "t"
        return rng.pick(COMMON[rng.below(len(COMMON))]), "c"


FAMILIES = [[family.split(" ") for family in text.split("|")] for _, _, _, text in TOPICS]
COMMON_FAMILY = {form: i for i, family in enumerate(COMMON) for form in family}


def fill(rng: Rng, template: str, words: Words, refs: int = 0) -> list:
    """Tokens [before, word, after, tag] of one template; the tag "c" marks a COMMON word."""
    tokens = []
    for part in template.split(" "):
        m = SLOT.search(part)
        if not m:
            tokens.append(["", part, "", "l"])
            continue
        before, after, slot = part[:m.start()], part[m.end():], m.group(1)
        tag = "x"
        if slot == "w":
            word, tag = words.content(rng)
        elif slot == "f":
            word = rng.pick(FUNCTION_WORDS)
        elif slot == "n":
            word = str(2 + rng.below(98))
        elif slot == "d":
            word = f"{1 + rng.below(9)}.{rng.below(10)}"
        elif slot == "s":
            word = f"0.{10 + rng.below(90)}"
        elif slot == "p":
            word = f"{10 + rng.below(90)}.{rng.below(10)}"
        elif slot == "r":
            word = f"[{1 + rng.below(refs)}]"
        else:
            a = 1 + rng.below(refs)
            b = 1 + rng.below(refs - 1)
            b = b + 1 if b >= a else b
            tokens.append([before + "[", str(min(a, b)) + ",", "", "x"])
            word, before = str(max(a, b)) + "]", ""
        tokens.append([before, word, after, tag])
    return tokens


def sentence(rng: Rng, templates: list, words: Words, refs: int = 0) -> list:
    tokens = fill(rng, rng.pick(templates), words, refs)
    first = tokens[0]
    first[1] = first[1][:1].upper() + first[1][1:]
    return tokens


def texts(tokens: list) -> list:
    return [before + word + after for before, word, after, _ in tokens]


def plain(tokens: list) -> str:
    return " ".join(texts(tokens))


def wrap(words: list, width: int, rng: Rng = None, num: int = 0, den: int = 1) -> list:
    """Greedy wrapping at spaces; with a generator, long words are split by a soft hyphen."""
    lines, line = [], ""
    for word in words:
        if not line:
            line = word
            continue
        if len(line) + 1 + len(word) <= width:
            line += " " + word
            continue
        room = width - len(line) - 1
        core = word.rstrip(".,")
        if (rng is not None and len(word) - len(core) <= 1 and len(core) >= 9 and core.isalpha()
                and core.islower() and core.isascii() and room >= 4 and rng.chance(num, den)):
            cut = min(room - 1, len(core) - 3)
            lines.append(line + " " + core[:cut] + "-")
            line = core[cut:] + word[len(core):]
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def wrap_entry(text: str) -> list:
    """Reference entries break at spaces; no continuation line starts like a marker or a number."""
    words, lines, i = text.split(" "), [], 0
    while i < len(words):
        line = [words[i]]
        length = len(words[i])
        i += 1
        while i < len(words) and length + 1 + len(words[i]) <= WIDTH:
            length += 1 + len(words[i])
            line.append(words[i])
            i += 1
        while i < len(words) and words[i][0] in "[0123456789" and len(line) > 1:
            i -= 1
            line.pop()
        lines.append(" ".join(line))
    return lines


def paginate(blocks: list) -> list:
    """Blocks are (lines, heading); an entry never splits and a heading never ends a page."""
    pages = [[]]
    for i, (lines, heading) in enumerate(blocks):
        need = len(lines) + (len(blocks[i + 1][0]) if heading and i + 1 < len(blocks) else 0)
        if pages[-1] and len(pages[-1]) + need > PAGE_LINES:
            pages.append([])
        pages[-1].extend(lines)
    return pages


class Paper:
    def __init__(self, name: str = ""):
        self.name = name            # planted case, or "" for a drawn paper
        self.planted = bool(name)
        self.k = -1
        self.id = ""
        self.versions = []
        self.listed = []
        self.cross_later = []
        self.cited = 0


class Version:
    def __init__(self, v: int, day: dt.date):
        self.v = v
        self.day = day
        self.withdrawn = False
        self.body = "new"           # "new", "same" (as the version before), "none", "empty"
        self.entries = []
        self.analysis = None


class Corpus:
    def __init__(self, seed: int, weeks: int, start: dt.date, feeds: list, daily: int, topics: int):
        self.seed, self.weeks, self.start, self.feeds = seed, weeks, start, feeds
        self.daily, self.topics = daily, topics
        self.days = [start + dt.timedelta(days=i) for i in range(7 * weeks)]
        self.end = self.days[-1]
        self.announce = [d for d in self.days if d.weekday() < 5]
        self.turn = (start.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        after = [d for d in self.announce if d >= self.turn]
        before = [d for d in reversed(self.announce) if d < self.turn]
        self.A = after
        self.B = [None] + before
        self.quiet = (self.turn, after[7])
        self.late_from = next(d for d in self.announce if d > self.turn)
        self.fade_after = self.B[7]
        self.papers = []
        self.targets = []
        self.published = set()
        self.lists = {d: {f: [] for f in feeds} for d in self.days}
        self.faults = {d: {"gone": [], "nopdf": [], "bad": [], "junk": []} for d in self.days}
        self.events = {}
        self.plan = {}
        self.roles = {}
        self.event_rng = Rng(seed, "events")
        self.check_topics()
        self.draw_pool()
        self.draw_authors()
        self.draw_teams()

    # -- the world before the first day --

    def topic_weights(self, fi: int, day: dt.date) -> list:
        out = []
        for t in range(self.topics):
            _, h1, h2, _ = TOPICS[t]
            w = (3 if h1 % len(self.feeds) == fi else 0) + (1 if h2 % len(self.feeds) == fi else 0)
            if t == self.topics - 2:
                w *= 1 if day > self.fade_after else 20
            elif t == self.topics - 1:
                w *= 0 if day < self.late_from else 20
            else:
                w *= 20
            if w:
                out.append((t, w))
        return out

    def check_topics(self) -> None:
        for fi, feed in enumerate(self.feeds):
            for day in self.announce:
                if not self.topic_weights(fi, day):
                    raise UsageError(f"feed {feed} has no active topic on {day.isoformat()}")

    def draw_pool(self) -> None:
        rng = Rng(self.seed, "external")
        first = self.start - dt.timedelta(days=3)
        limit = min("2607", f"{first.year % 100:02d}{first.month - 1:02d}" if first.month > 1
                    else f"{first.year % 100 - 1:02d}12")
        five = [f"{y:02d}{m:02d}" for y in range(15, 27) for m in range(1, 13) if f"{y:02d}{m:02d}" <= limit]
        four = [f"{y:02d}{m:02d}" for y in range(7, 13) for m in range(1, 13) if f"{y:02d}{m:02d}" >= "0704"]
        old = ["96", "97", "98", "99", "00", "01", "02"]
        ids, seen = [], set()

        def add(make, count):
            while count:
                value = make()
                if value not in seen:
                    seen.add(value)
                    ids.append(value)
                    count -= 1

        add(lambda: f"{rng.pick(five)}.{90001 + rng.below(9999)}", 240)
        add(lambda: f"{rng.pick(four)}.{9000 + rng.below(1000)}", 40)
        add(lambda: f"{rng.pick(['cs', 'math', 'physics'])}/{rng.pick(old)}{1 + rng.below(12):02d}"
                    f"{901 + rng.below(99)}", 20)
        rng.shuffle(ids)
        weights = [1000 // (rank + 1) for rank in range(len(ids))]
        # E2 of the zoo paper is named in its body and must stay out of every reference list
        last = max(i for i, value in enumerate(ids) if "." in value and len(value) == 10)
        weights[last] = 0
        self.pool, self.pool_totals, self.e2 = ids, running(weights), ids[last]
        five_at = [i for i, value in enumerate(ids) if "." in value and len(value) == 10 and i != last]
        self.e1 = ids[five_at[self.event_rng.weighted([weights[i] for i in five_at])]]

    def syllables(self, rng: Rng) -> str:
        text = "".join(rng.pick(ONSETS) + rng.pick(VOWELS) + rng.pick(CODAS) for _ in range(2 + rng.below(2)))
        return text[:1].upper() + text[1:]

    def draw_authors(self) -> None:
        rng = Rng(self.seed, "authors")
        names, seen = [], set()
        while len(names) < 40 * self.daily:
            given, family = self.syllables(rng), self.syllables(rng)
            if rng.chance(2, 100):
                spots = [i for i, ch in enumerate(family) if ch in ACCENTED]
                if spots:
                    i = rng.pick(spots)
                    family = family[:i] + ACCENTED[family[i]] + family[i + 1:]
            name = f"{given} {family}"
            if fold(name) not in seen:
                seen.add(fold(name))
                names.append(name)
        while True:
            given, family = self.syllables(rng), self.syllables(rng)
            if "e" in family[1:] and fold(f"{given} {family}") not in seen:
                break
        i = family.index("e", 1)
        self.nfd_names = (f"{given} {family[:i]}\u00e9{family[i + 1:]}", f"{given} {family[:i]}e\u0301{family[i + 1:]}")
        self.authors = names

    def draw_teams(self) -> None:
        rng = Rng(self.seed, "teams")
        by_topic = [[i for i in range(len(self.authors)) if i % self.topics == t] for t in range(self.topics)]
        self.teams = []
        for j in range(len(self.authors) // 3):
            topic = j if j < self.topics else rng.below(self.topics)
            size = 1 + rng.weighted(TEAM_SIZE_WEIGHTS)
            members = rng.sample(by_topic[topic], min(size, len(by_topic[topic])))
            begin = self.start
            # every topic keeps one team from the first day, so a paper always finds one
            if j >= self.topics and self.weeks > 1 and not rng.chance(60, 100):
                begin = self.start + dt.timedelta(days=7 * (1 + rng.below(self.weeks - 1)))
            self.teams.append((topic, begin, members))

    # -- the planted cases --

    def plant(self) -> None:
        A, B, feeds = self.A, self.B, self.feeds
        f0, f1, f2 = feeds[0], feeds[1], feeds[min(2, len(feeds) - 1)]
        tuesday = self.turn - dt.timedelta(days=1)
        while tuesday.weekday() != 1:
            tuesday -= dt.timedelta(days=1)
        named = {}

        def put(day, feed, name, **spec):
            paper = Paper(name)
            self.plan.setdefault((day, feed), []).append((paper, spec))
            named.setdefault(name, []).append(paper)
            return paper

        def role(day, feed, name, **spec):
            self.roles.setdefault((day, feed), []).append(dict(spec, case=name))

        def fault(kind, paper, first, last):
            day = first
            while day <= last:
                self.faults[day][kind].append(paper)
                day += dt.timedelta(days=1)

        def event(day, paper, *action):
            self.events.setdefault(day, []).append((paper, action))

        cocite = [put(B[13], f0, "cocite"), put(B[13], f1, "cocite"), put(B[12], f2, "cocite")]
        twin = put(B[12], f0, "twins", topic=2)
        put(B[12], f0, "twins", topic=2, copy=twin)
        put(B[11], f0, "zoo", style="A")
        hub = []
        for i in range(7):
            hub.append(put(B[11 - i], f0 if i % 2 == 0 else f1, "withdrawn_hub", topic=1, member=i))
        put(B[10], f1, "prototype", topic=0)
        put(B[10], f0, "no_text")
        put(B[9], f0, "unicode_title")
        put(B[9], f0, "tex_title")
        junk = put(B[8], f1, "junk")
        put(B[8], f1, "contents_decoy")
        body = put(B[7], f0, "body_only")
        early = put(B[6], f0, "withdrawn_early")
        sunday = tuesday - dt.timedelta(days=2)
        put(tuesday, f1, "sunday_monday", published=(sunday, 23 * 3600 + 3000))
        put(tuesday, f1, "sunday_monday", published=(tuesday - dt.timedelta(days=1), 0))
        link = put(B[4], f0, "late_link")
        heals = put(B[3], f0, "pdf_heals")
        put(B[2], f0, "nfd_author", author=self.nfd_names[0])
        put(B[1], f0, "nfd_author", author=self.nfd_names[1])
        late = put(A[0], f2, "api_late", first_day=A[5], categories=[f2, f1],
                   published=(self.turn - dt.timedelta(days=1), 0, 86400))
        cited = put(A[3], f1, "late_cited", primary="math.OC",
                    published=(self.turn - dt.timedelta(days=20), 0, 86400))
        prototype = named["prototype"][0]

        for feed in (f1, f2):
            role(B[7], feed, "cocite", cites=cocite)
        role(B[6], f0, "withdrawn_hub", cites=[hub[3]])
        role(B[6], f2, "withdrawn_hub", cites=[hub[3]])
        role(B[3], f1, "withdrawn_hub", cites=[hub[3]])
        for day, feed, style, pattern in ((B[4], f1, "A", 0), (B[3], f2, "B", 1), (B[2], f1, "C", 0),
                                          (B[1], f2, "A", 2)):
            role(day, feed, "late_cited", cites=[cited], style=style, pattern=pattern)

        event(A[2], prototype, "version", "retitled")
        event(A[2], body, "version", "body")
        event(A[2], link, "cross", f1)
        event(A[5], hub[3], "version", "withdraw")
        event(A[5], late, "list", f1)
        event(B[5], early, "version", "withdraw")
        fault("gone", late, A[0], A[0])
        fault("nopdf", heals, B[3], A[3] - dt.timedelta(days=1))
        # until the withdrawal, so that no cycle in between can analyse the paper
        fault("bad", early, B[6], B[5] - dt.timedelta(days=1))
        fault("junk", junk, B[8], self.end)

        self.named = named
        self.burst_day = B[9] if self.daily >= 20 else None
        self.clean = (A[1], A[7])
        self.case_days = {
            "api_late": [A[0], A[5]], "body_only": [B[7], A[2]], "clean_days": [A[1], A[7]],
            "cocite": [B[13], B[12], B[7]], "contents_decoy": [B[8]], "junk": [B[8], self.end],
            "late_cited": [B[4], B[3], B[2], B[1], A[3]], "late_link": [B[4], A[2]],
            "nfd_author": [B[2], B[1]], "no_text": [B[10]],
            "pdf_heals": [B[3], A[3] - dt.timedelta(days=1), A[3]], "prototype": [B[10], A[2]],
            "sunday_monday": [tuesday], "tex_title": [B[9]], "twins": [B[12]], "unicode_title": [B[9]],
            "withdrawn_early": [B[6], B[5]], "withdrawn_hub": sorted(set(B[5:12]) | {B[3], A[5]}),
            "zoo": [B[11]],
        }

    # -- day by day --

    def generate(self) -> None:
        self.plant()
        weights = [FEED_WEIGHTS[i] if i < len(FEED_WEIGHTS) else 10 for i in range(len(self.feeds))]
        whole = sum(weights)  # ints
        for day in self.announce:
            rng = Rng(self.seed, f"day:{day.isoformat()}")
            total = self.daily + rng.below(7) - 3
            # largest remainder: floors first, the rest to the largest remainders, ties to the earlier feed
            shares = [total * w // whole for w in weights]
            order = sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % whole), i))
            for i in order[:total - sum(shares)]:  # ints
                shares[i] += 1
            if day == self.burst_day:
                shares[1] = BURST
            today = []
            for fi, feed in enumerate(self.feeds):
                planted = self.plan.get((day, feed), [])
                roles = self.roles.get((day, feed), [])
                for paper, spec in planted:
                    today.append(self.new_paper(paper, day, feed, spec))
                for j in range(max(shares[fi] - len(planted), len(roles))):
                    spec = roles[j] if j < len(roles) else {}
                    if day in self.clean:
                        spec = dict(spec, clean=True)
                    if day == self.burst_day and fi == 1:
                        spec = dict(spec, inside=True)
                    today.append(self.new_paper(Paper(), day, feed, spec))
            for paper, action in sorted(self.events.get(day, []), key=lambda item: item[0].k):
                self.apply(paper, day, action)
            self.targets.extend(p for p in today if not p.planted)

    def next_announcement(self, day: dt.date):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        if day > self.end or self.quiet[0] <= day <= self.quiet[1]:
            return None
        return day

    def listing(self, day: dt.date, feed: str, paper: Paper, v: int, kind: str) -> None:
        self.lists[day][feed].append((paper, v, kind))
        if feed not in paper.listed:
            paper.listed.append(feed)

    def new_paper(self, paper: Paper, day: dt.date, feed: str, spec: dict) -> Paper:
        paper.k = len(self.papers)
        self.papers.append(paper)
        rng = Rng(self.seed, f"paper:{paper.k}")
        fi = self.feeds.index(feed)
        free = not paper.planted and "cites" not in spec
        loose = free and not spec.get("clean")
        weights = self.topic_weights(fi, day)
        if "topic" in spec:
            topics = [spec["topic"]]
        else:
            topics = [weights[rng.weighted([w for _, w in weights])][0]]
            rest = [(t, w) for t, w in weights if t != topics[0]]
            if rng.chance(30, 100) and rest:
                topics.append(rest[rng.weighted([w for _, w in rest])][0])
        paper.topics = topics
        if "primary" in spec:
            paper.primary = spec["primary"]
        elif loose and not spec.get("inside") and rng.chance(5, 100):
            paper.primary = rng.pick([c for c in OUTSIDE if c not in self.feeds])
        else:
            paper.primary = feed
        categories = [paper.primary] if paper.primary == feed else [paper.primary, feed]
        self.listing(day, feed, paper, 1, "new" if paper.primary == feed else "cross")
        if loose and paper.primary == feed and rng.chance(18, 100):
            second = rng.pick([f for f in self.feeds if f != feed])
            categories.append(second)
            self.listing(day, second, paper, 1, "cross")
        paper.categories = spec.get("categories", categories)

        if "published" in spec:
            base, low, span = (spec["published"] + (600,))[:3]
        else:
            ds = [55, 30, 15]
            if spec.get("clean") and day.weekday() > 0:
                ds = ds[:day.weekday()]
            base, low, span = day - dt.timedelta(days=1 + rng.weighted(ds)), 0, 86400
        while True:
            moment = at(base, low + rng.below(span))
            if moment not in self.published:
                break
        self.published.add(moment)
        paper.published = moment

        active = [t for t in self.teams if t[1] <= day]
        while True:
            team = rng.pick([t for t in active if t[0] == topics[0]])
            names = [self.authors[i] for i in team[2]]
            # the second twin must not share its authors with the first
            if "copy" not in spec or names != spec["copy"].authors:
                break
        if rng.chance(15, 100):
            extra = self.authors[rng.pick(rng.pick(active)[2])]
            if extra not in names:
                names.append(extra)
        if "author" in spec:
            names.append(spec["author"])
        paper.authors = names
        paper.style = spec.get("style") or "ABC"[rng.weighted([45, 30, 25])]
        paper.comment = None
        if rng.chance(7, 10):
            paper.comment = f"{6 + rng.below(25)} pages, {1 + rng.below(12)} figures"
        paper.journal_ref = paper.doi = None
        if rng.chance(3, 100):
            first = 1 + rng.below(400)
            paper.journal_ref = (f"{rng.pick(JOURNALS)} {1 + rng.below(60)} ({moment.year}) "
                                 f"{first}-{first + 8 + rng.below(30)}")
            paper.doi = self.doi(rng)
        paper.words = Words(topics, (19, 20), 20) if paper.name == "prototype" else Words(topics)
        paper.keywords = self.keywords(rng, paper.words)
        paper.novelty = 1 + rng.weighted(NOVELTY_WEIGHTS)
        paper.tex = free and rng.chance(1, 100)
        paper.day = day
        if "copy" in spec:
            twin = spec["copy"]
            paper.keywords, paper.novelty, paper.comment = twin.keywords, twin.novelty, twin.comment
        if paper.name == "withdrawn_hub" and spec["member"]:
            paper.keywords = self.named["withdrawn_hub"][0].keywords
        if free:
            self.schedule(rng, paper, day)
        paper.versions.append(self.first_version(paper, day, spec))
        return paper

    def schedule(self, rng: Rng, paper: Paper, day: dt.date) -> None:
        taken = []
        if rng.chance(1, 100):
            when = self.next_announcement(day + dt.timedelta(days=3 + rng.below(13)))
            if when:
                self.events.setdefault(when, []).append((paper, ("version", "withdraw")))
                return
        elif rng.chance(9, 100):
            when = self.next_announcement(day + dt.timedelta(days=2 + rng.below(19)))
            kind = ("body", "revised", "retitled")[rng.weighted([50, 35, 15])]
            if when:
                taken.append(when)
                self.events.setdefault(when, []).append((paper, ("version", kind)))
                if rng.chance(2, 10):
                    later = self.next_announcement(when + dt.timedelta(days=2 + rng.below(19)))
                    kind = ("body", "revised", "retitled")[rng.weighted([50, 35, 15])]
                    if later:
                        taken.append(later)
                        self.events.setdefault(later, []).append((paper, ("version", kind)))
        if rng.chance(2, 100):
            when = self.next_announcement(day + dt.timedelta(days=3 + rng.below(8)))
            feeds = [f for f in self.feeds if f not in paper.listed]
            if when and feeds and when not in taken:
                self.events.setdefault(when, []).append((paper, ("cross", rng.pick(feeds))))

    def apply(self, paper: Paper, day: dt.date, action: tuple) -> None:
        if action[0] == "version":
            version = self.next_version(paper, day, action[1])
            paper.versions.append(version)
            for feed in paper.listed:
                self.listing(day, feed, paper, version.v, "replace" if feed == paper.primary else "replace-cross")
        else:
            if action[0] == "cross":
                paper.cross_later.append({"day": day.isoformat(), "feed": action[1]})
            self.listing(day, action[1], paper, paper.versions[-1].v, "cross")

    # -- versions --

    def keywords(self, rng: Rng, words: Words) -> list:
        out, seen = [], set()
        count = 4 + rng.below(4)
        while len(out) < count:
            phrase = " ".join(words.topic_word(rng) for _ in range(1 + rng.weighted([3, 5, 2])))
            if rng.chance(1, 5):
                phrase = phrase[0].upper() + phrase[1:]
            if phrase.lower() not in seen:
                seen.add(phrase.lower())
                out.append(phrase)
        return out

    def doi(self, rng: Rng) -> str:
        return f"10.9{100 + rng.below(900)}/{rng.pick(DOI_WORDS)}.{10 + rng.below(990)}"

    def title(self, rng: Rng, words: Words, tex: bool) -> str:
        tokens = fill(rng, rng.pick(TITLES), words)
        parts = [title_case(word, i == 0) for i, word in enumerate(texts(tokens))]
        if tex:
            parts.insert(0, rng.pick(TEX))
        return " ".join(parts)

    def abstract(self, rng: Rng, words: Words) -> list:
        roles = ["problem"]
        if rng.chance(1, 2):
            roles.append("gap")
        roles.append("proposal")
        if rng.chance(1, 2):
            roles.append("method")
        roles.append("result")
        if rng.chance(1, 3):
            roles.append("result")
        roles.append("conclusion")
        return [(role, sentence(rng, ABSTRACT[role], words)) for role in roles]

    def set_abstract(self, version: Version, rng: Rng) -> None:
        words = [w for _, tokens in version.sentences for w in texts(tokens)]
        hyphenate = rng.chance(1, 10)
        version.abstract = "\n".join(wrap(words, ABSTRACT_WIDTH, rng if hyphenate else None, 1, 2))

    def analysis(self, rng: Rng, paper: Paper, version: Version) -> dict:
        found = {}
        for role, tokens in version.sentences:
            found.setdefault(role, fold(plain(tokens)))
        summary = [f'This paper is titled "{fold(version.title)}".', found["problem"], found["proposal"]]
        for role in ("result", "conclusion"):
            if rng.chance(1, 2):
                summary.append(found[role])
        contributions = [plain(sentence(rng, CONTRIBUTIONS, paper.words)) for _ in range(2 + rng.below(3))]
        method = [found.get("method", found["proposal"])]
        if rng.chance(1, 2):
            method.append(plain(sentence(rng, METHOD_EXTRA, paper.words)))
        limits = None if rng.chance(1, 10) else plain(sentence(rng, LIMITATIONS, paper.words))
        return {"summary": " ".join(summary), "key_contributions": contributions,
                "methodology": " ".join(method), "limitations": limits,
                "keywords": paper.keywords, "novelty_score": paper.novelty}

    def first_version(self, paper: Paper, day: dt.date, spec: dict) -> Version:
        version = Version(1, spec.get("first_day", day))
        version.updated = paper.published
        rng = Rng(self.seed, f"text:{paper.k}:1")
        version.rng = rng
        words = paper.words
        if "copy" in spec:
            twin = spec["copy"].versions[0]
            version.title, version.sentences = twin.title, twin.sentences
        elif paper.name == "withdrawn_hub":
            version.title, version.sentences = self.hub_text(rng, paper, spec["member"])
        else:
            version.title = self.title(rng, words, paper.tex)
            version.sentences = self.abstract(rng, words)
        if paper.name == "unicode_title":
            t1, t2, t3 = (title_case(words.topic_word(rng), False) for _ in range(3))
            version.title = (f"\u201cCo\u00f6perative\u201d Nai\u0308ve {t1} \u2013 \u03b2-Weighted "
                             f"Re\ufb01nement for {t2} {t3}")
            version.sentences[-1] = ("conclusion", fill(rng, "The \ufb01ndings carry over to other "
                                                             "work\ufb02ows of the same kind.", words))
        if paper.name == "tex_title":
            t1, t2 = (title_case(words.topic_word(rng), False) for _ in range(2))
            version.title = f"$\\alpha$-Stable {t1} {t2} in $O(n \\log n)$ Time"
        self.set_abstract(version, rng)
        if "copy" in spec:
            version.abstract, version.analysis = twin.abstract, twin.analysis
        else:
            version.analysis = self.analysis(rng, paper, version)
        refs = Rng(self.seed, f"refs:{paper.k}:1")
        if paper.name == "no_text":
            version.body = "empty"
        elif paper.name == "zoo":
            version.entries = self.zoo_entries(refs, paper, day)
        else:
            version.entries = self.draw_entries(refs, paper, day)
            for target in spec.get("cites", []):
                entry = self.entry(refs, paper, "internal", target)
                if "pattern" in spec:
                    entry["pattern"] = spec["pattern"]
                version.entries.insert(refs.below(len(version.entries) + 1), entry)
        return version

    def hub_text(self, rng: Rng, paper: Paper, member: int):
        if member == 0:
            while True:
                title = self.title(rng, paper.words, False)
                sentences = self.abstract(rng, paper.words)
                spots = [(i, j) for i, (_, tokens) in enumerate(sentences)
                         for j, token in enumerate(tokens) if token[3] == "c"]
                if len(spots) >= 7:
                    break
            self.hub_base = (title, sentences, spots)
        title, base, spots = self.hub_base
        i, j = spots[member]
        sentences = [(role, [list(token) for token in tokens]) for role, tokens in base]
        token = sentences[i][1][j]
        old = token[1].lower()
        while True:
            family = rng.below(len(COMMON))
            if family != COMMON_FAMILY[old]:
                break
        word = rng.pick(COMMON[family])
        token[1] = word[:1].upper() + word[1:] if j == 0 else word
        return title, sentences

    def next_version(self, paper: Paper, day: dt.date, kind: str) -> Version:
        before = paper.versions[-1]
        version = Version(before.v + 1, day)
        rng = Rng(self.seed, f"text:{paper.k}:{version.v}")
        version.rng = rng
        version.updated = at(day - dt.timedelta(days=1), rng.below(86400))
        version.title, version.sentences, version.abstract = before.title, before.sentences, before.abstract
        if kind == "withdraw":
            version.withdrawn = True
            version.body = "none"
            return version
        version.analysis = before.analysis
        version.entries = before.entries
        version.body = "same"
        if kind == "body":
            refs = Rng(self.seed, f"refs:{paper.k}:{version.v}")
            entries = list(before.entries)
            inside = [i for i, e in enumerate(entries) if e["kind"] == "internal"]
            if inside:
                del entries[inside[-1]]
            taken = {e["target"].k for e in entries if e["kind"] == "internal"}
            for target in self.draw_targets(refs, paper, day, 1, taken):
                entries.append(self.entry(refs, paper, "internal", target))
            version.entries = entries
            version.body = "new"
            return version
        if kind == "retitled":
            version.title = self.title(rng, paper.words, paper.tex)
        else:
            sentences = list(before.sentences)
            if len(sentences) < 7 and rng.chance(1, 2):
                sentences.insert(len(sentences) - 1, ("result", sentence(rng, ABSTRACT["result"], paper.words)))
            else:
                i = 1 + rng.below(len(sentences) - 1)
                sentences[i] = (sentences[i][0], sentence(rng, ABSTRACT[sentences[i][0]], paper.words))
            version.sentences = sentences
            self.set_abstract(version, rng)
        version.analysis = self.analysis(rng, paper, version)
        return version

    # -- reference lists --

    def draw_targets(self, rng: Rng, paper: Paper, day: dt.date, count: int, taken: set) -> list:
        pool = self.targets
        count = min(count, (len(pool) - len(taken)) // 2)
        if count <= 0:
            return []
        first, mine, now = paper.topics[0], set(paper.topics), day.toordinal()
        weights = []
        for target in pool:
            if target.topics[0] == first:
                w = 12
            elif mine.intersection(target.topics):
                w = 5
            else:
                w = 1
            age = now - target.day.toordinal()
            w *= 3 if age <= 14 else 2 if age <= 45 else 1
            weights.append(w * (1 + target.cited))
        totals = running(weights)
        picks = []
        while len(picks) < count:
            target = pool[rng.weighted_cum(totals)]
            if target is not paper and target.k not in taken:
                taken.add(target.k)
                picks.append(target)
        for target in picks:
            target.cited += 1
        return picks

    def draw_entries(self, rng: Rng, paper: Paper, day: dt.date) -> list:
        kinds = [("internal", "external", "other")[rng.weighted([50, 15, 35])]
                 for _ in range(4 + rng.weighted(REF_COUNT_WEIGHTS))]
        wanted = kinds.count("internal") if len(self.targets) >= 10 else 0
        targets = self.draw_targets(rng, paper, day, wanted, set())
        entries, used = [], set()
        for kind in kinds:
            if kind == "internal" and targets:
                entries.append(self.entry(rng, paper, "internal", targets.pop(0)))
            elif kind == "other":
                entries.append(self.entry(rng, paper, "other"))
            else:
                while True:
                    ext = self.pool[rng.weighted_cum(self.pool_totals)]
                    if ext not in used:
                        break
                used.add(ext)
                entries.append(self.entry(rng, paper, "external", ext))
        return entries

    def entry(self, rng: Rng, paper: Paper, kind: str, target=None) -> dict:
        count = 1 + rng.weighted([25, 30, 20, 25])
        people = []
        while len(people) < min(count, 3):
            given, family = fold(rng.pick(self.authors)).split(" ")
            name = f"{given[0]}. {family}"
            if name not in people:
                people.append(name)
        if count == 4:
            authors = f"{people[0]} et al."
        elif count == 3:
            authors = f"{people[0]}, {people[1]}, and {people[2]}"
        else:
            authors = " and ".join(people)
        title = plain(fill(rng, rng.pick(REF_TITLES), paper.words))
        first = 1 + rng.below(9000)
        entry = {"kind": kind, "target": target, "authors": authors, "title": title[:1].upper() + title[1:],
                 "pattern": rng.weighted([6, 2, 2]) if kind != "other" else int(rng.chance(1, 3)),
                 "k": 1 + rng.below(3), "year": 2015 + rng.below(12), "venue": rng.pick(VENUES),
                 "journal": rng.pick(JOURNALS), "vol": 1 + rng.below(60), "no": 1 + rng.below(12),
                 "pages": f"{first}-{min(9999, first + 1 + rng.below(40))}", "doi": self.doi(rng),
                 "primary": rng.pick(self.feeds + OUTSIDE)}
        return entry

    def zoo_entries(self, rng: Rng, paper: Paper, day: dt.date) -> list:
        x = self.draw_targets(self.event_rng, paper, day, 4, set())
        fixed = [("internal", x[0], "arXiv preprint arXiv:{id}v2, {year}."),
                 ("duplicate", x[0], "arXiv:{id}, {year}."),
                 ("internal", x[1], "https://arxiv.org/abs/{id}"),
                 ("external", self.e1, "http://arxiv.org/pdf/{id}v1"),
                 ("external", "cs/9812901", "arXiv:cs/9812901, 1998."),
                 ("external", "math/0001901", "https://arxiv.org/abs/math/0001901v2"),
                 ("external", "0911.9412", "arXiv:0911.9412, 2009."),
                 ("self", paper, "arXiv:{id}, {year}."),
                 ("internal", x[2], "Technical report (arXiv:{id})."),
                 ("internal", x[3], "CoRR, abs/{id}, {year}."),
                 ("broken", x[1], "arXiv:{yymm},{seq}, {year}."),
                 ("broken", x[2], "arXiv: {yymm}. {seq}, {year}."),
                 ("other", None, "{journal}, {vol}({no}):{pages}, {year}. doi:{doi}"),
                 ("other", None, "In {venue}, pages {pages}, {year}.")]
        entries = []
        for kind, target, text in fixed:
            entry = self.entry(rng, paper, "other")
            entry.update(kind=kind, target=target, fixed=text)
            entries.append(entry)
        return entries

    def entry_id(self, entry: dict):
        target = entry["target"]
        if entry["kind"] in ("other",) or target is None:
            return None
        if entry["kind"] == "broken":
            return None
        return target if isinstance(target, str) else target.id

    def render_entry(self, n: int, entry: dict, style: str) -> str:
        target = entry["target"]
        ident = self.entry_id(entry)
        fields = dict(entry, id=ident)
        if isinstance(target, Paper):
            fields.update(year=target.published.year, primary=target.primary, id=target.id,
                          yymm=target.id[:4], seq=target.id[5:])
        elif ident and "." in ident:
            fields["year"] = 2000 + int(ident[:2])
        elif ident:
            yy = int(ident.split("/")[1][:2])
            fields["year"] = (1900 if yy > 50 else 2000) + yy
        if "fixed" in entry:
            tail = entry["fixed"].format(**fields)
        elif ident is None:
            tail = OTHER_PATTERNS[style][entry["pattern"]].format(**fields)
        else:
            pattern = entry["pattern"]
            if "/" in ident:
                pattern = OLD_STYLE_PATTERN.get((style, pattern), pattern)
            tail = ID_PATTERNS[style][pattern].format(**fields)
        authors, title, year = entry["authors"], entry["title"], fields["year"]
        if style == "A":
            lead = authors if authors.endswith(".") else authors + "."
            return f"[{n}] {lead} {title}. {tail}"
        if style == "B":
            return f"{n}. {authors} ({year}). {title}. {tail}"
        return f'[{n}] {authors}, "{title}," {tail}'

    # -- after the last day --

    def assign_ids(self) -> None:
        counts = {}
        for paper in sorted(self.papers, key=lambda p: p.published):
            month = f"{paper.published.year % 100:02d}{paper.published.month:02d}"
            counts[month] = counts.get(month, 0) + 1
            if counts[month] > 9999:
                raise UsageError(f"more than 9999 papers in month {month}")
            paper.id = f"{month}.{90000 + counts[month]}"

    def layout(self, paper: Paper, version: Version, body: list) -> list:
        rng = version.rng
        stamp = version.updated
        blocks = [([f"arXiv:{paper.id}v{version.v} [{paper.primary}] {stamp.day} {MONTHS[stamp.month - 1]} "
                    f"{stamp.year}"], False)]
        lines = wrap(fold(version.title).split(" "), WIDTH)
        lines += wrap(fold(", ".join(paper.authors)).split(" "), WIDTH)
        lines.append("Abstract")
        lines += wrap([fold(w) for _, tokens in version.sentences for w in texts(tokens)], WIDTH, rng, 1, 3)
        if paper.name == "contents_decoy":
            lines += ["Contents"] + [heading for heading, _ in body] + ["References"]
        blocks += [([line], False) for line in lines]
        for heading, paragraphs in body:
            blocks.append(([heading], True))
            for words in paragraphs:
                blocks += [([line], False) for line in wrap(words, WIDTH, rng, 1, 3)]
        blocks.append(([REF_HEADINGS[paper.style]], True))
        for n, entry in enumerate(version.entries, 1):
            blocks.append((wrap_entry(self.render_entry(n, entry, paper.style)), False))
        return paginate(blocks)

    def body(self, paper: Paper, version: Version) -> list:
        rng, refs = version.rng, len(version.entries)
        sections = []
        for choices in HEADINGS:
            paragraphs = []
            for _ in range(2 + rng.below(2)):
                words = []
                for _ in range(3 + rng.below(3)):
                    words += texts(sentence(rng, BODY, paper.words, refs))
                paragraphs.append(words)
            sections.append((rng.pick(choices), paragraphs))
        if paper.name == "zoo":
            sections[0][1][0].extend(f"This extends the analysis of arXiv:{self.e2} to the routed setting."
                                     .split(" "))
        return sections

    def finish(self, paper: Paper) -> None:
        body = None
        for version in paper.versions:
            if version.body == "new":
                body = self.body(paper, version)
            if version.body in ("new", "same"):
                version.pages = self.layout(paper, version, body)
            else:
                version.pages = [[]] if version.body == "empty" else None
            version.refs = [{"n": n, "id": self.entry_id(e), "kind": e["kind"]}
                            for n, e in enumerate(version.entries, 1)]
            cites, other = [], 0
            for ref in version.refs:
                if ref["id"] is None:
                    other += 1
                elif ref["id"] != paper.id and ref["id"] not in cites:
                    cites.append(ref["id"])
            version.cites, version.other_refs = cites, other

    def paper_record(self, paper: Paper) -> dict:
        versions = []
        for version in paper.versions:
            versions.append({
                "v": version.v, "day": version.day.isoformat(), "updated": instant(version.updated),
                "title": version.title, "abstract": version.abstract, "categories": paper.categories,
                "comment": WITHDRAWAL if version.withdrawn else paper.comment,
                "journal_ref": paper.journal_ref, "doi": paper.doi, "withdrawn": version.withdrawn,
                "pages": version.pages, "refs": version.refs, "cites": version.cites,
                "other_refs": version.other_refs, "analysis": version.analysis})
        return {"id": paper.id, "primary": paper.primary, "published": instant(paper.published),
                "authors": paper.authors, "topics": paper.topics, "style": paper.style,
                "cross_later": paper.cross_later, "versions": versions}

    def planted_record(self) -> dict:
        def ids(papers):
            return [p.id for p in papers]

        def by_day(papers):
            return [p.id for p in sorted(papers, key=lambda p: (p.day, p.id))]

        def citers(target):
            return by_day([p for p in self.papers if not p.planted and any(
                e["target"] is target for v in p.versions for e in v.entries)])

        days = {name: [d.isoformat() for d in value] for name, value in self.case_days.items()}
        n = self.named
        hub = n["withdrawn_hub"]
        out = {name: {"days": days[name], "id": n[name][0].id}
               for name in ("api_late", "body_only", "contents_decoy", "junk", "no_text", "pdf_heals",
                            "prototype", "tex_title", "unicode_title", "withdrawn_early", "zoo")}
        out["prototype"]["topic"] = 0
        out["late_link"] = {"days": days["late_link"], "feed": self.feeds[1], "id": n["late_link"][0].id}
        out["clean_days"] = {"days": days["clean_days"]}
        out["cocite"] = {"citers": citers(n["cocite"][0]), "days": days["cocite"], "ids": ids(n["cocite"])}
        out["late_cited"] = {"citers": citers(n["late_cited"][0]), "days": days["late_cited"],
                             "id": n["late_cited"][0].id}
        out["withdrawn_hub"] = {"citers": citers(hub[3]), "cluster": ids(hub), "days": days["withdrawn_hub"],
                                "id": hub[3].id}
        for name in ("nfd_author", "sunday_monday", "twins"):
            out[name] = {"days": days[name], "ids": ids(n[name])}
        out["fading_topic"] = {"topic": self.topics - 2}
        out["late_topic"] = {"topic": self.topics - 1}
        if self.burst_day:
            out["burst"] = {"count": BURST, "days": [self.burst_day.isoformat()], "feed": self.feeds[1]}
        return out

    def day_record(self, day: dt.date) -> dict:
        rank = {"new": 0, "cross": 1, "replace": 2, "replace-cross": 3}
        feeds = {feed: [[p.id, v, kind] for p, v, kind in sorted(
            self.lists[day][feed], key=lambda item: (rank[item[2]], item[0].id))] for feed in self.feeds}
        record = {"day": day.isoformat(), "feeds": feeds}
        for kind, papers in self.faults[day].items():
            record[kind] = sorted(p.id for p in papers)
        return record


def readme(corpus: Corpus, command: str, manifest: dict, records: list, days: list) -> bytes:
    versions = [v for r in records for v in r["versions"]]
    kinds = {}
    for v in versions:
        for ref in v["refs"]:
            kinds[ref["kind"]] = kinds.get(ref["kind"], 0) + 1
    replaced = len([v for v in versions if v["v"] > 1 and not v["withdrawn"]])
    withdrawn = len([v for v in versions if v["withdrawn"]])
    seen = {
        "api_late": "listed on the first day of the month, unknown to the API for a week, then cross-listed",
        "body_only": "a version 2 with new body and references, same title and abstract",
        "burst": "one feed announces more papers than one API batch holds",
        "clean_days": "days with new papers only, each in one feed",
        "cocite": "three papers cited together by the same two papers",
        "contents_decoy": "a contents block with a References line before the first section",
        "fading_topic": "a topic that almost stops after the seventh announcement day before the month turn",
        "junk": "an analysis with keywords as one string on every day: three tries, then failed",
        "late_cited": "cited four times before it is announced as a cross-list",
        "late_link": "a stored paper joins a second feed without a new version",
        "late_topic": "a topic that appears only after the month turn",
        "nfd_author": "one author written with a precomposed and with a decomposed accent",
        "no_text": "a PDF without text",
        "pdf_heals": "the PDF answers 404 for three tries and comes back later",
        "prototype": "a paper at the centre of its topic gets a new title",
        "sunday_monday": "published on both sides of a week boundary",
        "tex_title": "inline TeX in the title",
        "twins": "two papers with the same title, abstract, keywords and score",
        "unicode_title": "ligatures, accents, quotes, a dash and a Greek letter in the title",
        "withdrawn_early": "the analysis fails once, then the paper is withdrawn",
        "withdrawn_hub": "seven near-duplicates; the middle one, cited three times, is withdrawn",
        "zoo": "every way of writing an arXiv id in one reference list",
    }
    out = [f"# Corpus seed {corpus.seed}", "",
           "Synthetic arXiv announcements for `dev/standin.py` and `python -m app play`. Made with:", "",
           f"    {command}", "",
           "| Item | Value |", "|---|---|",
           f"| Range | {manifest['start']} to {manifest['end']} |",
           f"| Feeds | {', '.join(manifest['feeds'])} |",
           f"| Papers | {manifest['papers']} |", f"| Versions | {manifest['versions']} |",
           f"| Replacements | {replaced} |", f"| Withdrawals | {withdrawn} |",
           "| References | " + ", ".join(f"{kind} {kinds[kind]}" for kind in sorted(kinds)) + " |", "",
           f"From {corpus.B[1].isoformat()} on, no paper is waiting for another try.", "",
           "## Planted cases", "", "| Case | Ids | Days | What happens |", "|---|---|---|---|"]
    for name, case in sorted(manifest["planted"].items()):
        if "cluster" in case or "ids" in case:
            ids = case.get("cluster") or case["ids"]
        elif "id" in case:
            ids = [case["id"]]
        elif "topic" in case:
            ids = [f"topic {case['topic']}"]
        else:
            ids = [f"{case['count']} in {case['feed']}"] if "count" in case else []
        out.append(f"| {name} | {', '.join(ids)} | {', '.join(case.get('days', []))} | {seen[name]} |")
    out += ["", "## Fault windows", "", "| Id | Kind | First day | Last day |", "|---|---|---|---|"]
    windows = {}
    for day in days:
        for kind in ("gone", "nopdf", "bad", "junk"):
            for ident in day[kind]:
                first, _ = windows.get((ident, kind), (day["day"], None))
                windows[(ident, kind)] = (first, day["day"])
    for (ident, kind), (first, last) in sorted(windows.items(), key=lambda item: (item[1][0], item[0])):
        out.append(f"| {ident} | {kind} | {first} | {last} |")
    out += ["", "## Reference styles", ""]
    for style in "ABC":
        example = next(r for r in records if r["style"] == style and r["versions"][0]["refs"])
        lines = []
        for page in example["versions"][0]["pages"]:
            lines += page
        head = lines.index(REF_HEADINGS[style])
        entry = [lines[head + 1]]
        for line in lines[head + 2:]:
            if line.startswith("[2] ") or line.startswith("2. "):
                break
            entry.append(line)
        out.append(f"- {style}, heading `{REF_HEADINGS[style]}`: `{' '.join(entry)}`")
    return ("\n".join(out) + "\n").encode("utf-8")


def write(out: str, corpus: Corpus, command: str) -> tuple:
    records = [corpus.paper_record(p) for p in sorted(corpus.papers, key=lambda p: p.id)]
    days = [corpus.day_record(d) for d in corpus.days]
    manifest = {"seed": corpus.seed, "weeks": corpus.weeks, "daily": corpus.daily, "topics": corpus.topics,
                "start": corpus.start.isoformat(), "end": corpus.end.isoformat(), "feeds": corpus.feeds,
                "month_turns": [d.isoformat() for d in corpus.days if d.day == 1 and d > corpus.start],
                "papers": len(records), "versions": len([v for r in records for v in r["versions"]]),
                "planted": corpus.planted_record()}
    lines = b"".join(canonical(r) + b"\n" for r in records)
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=6) as gz:
        gz.write(lines)
    if os.path.isdir(out) and os.listdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "days"), exist_ok=True)
    files = {"corpus.json": canonical(manifest) + b"\n", "papers.jsonl.gz": buffer.getvalue(),
             "README.md": readme(corpus, command, manifest, records, days)}
    for day in days:
        files[os.path.join("days", day["day"] + ".json")] = canonical(day) + b"\n"
    for name, data in files.items():
        with open(os.path.join(out, name), "wb") as f:
            f.write(data)
    return manifest["papers"], manifest["versions"], len(days)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write a synthetic arXiv corpus.")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--start", default="2026-08-03")
    parser.add_argument("--feeds", default="cs.AI,cs.LG,cs.CL,cs.CV,stat.ML")
    parser.add_argument("--daily", type=int, default=22)
    parser.add_argument("--topics", type=int, default=18)
    args = parser.parse_args(argv)
    try:
        feeds = args.feeds.split(",")
        if args.seed < 0:
            raise UsageError("--seed must be 0 or more")
        if args.weeks < 1 or args.daily < 4:
            raise UsageError("--weeks must be at least 1 and --daily at least 4")
        if not 4 <= args.topics <= len(TOPICS):
            raise UsageError(f"--topics must lie between 4 and {len(TOPICS)}")
        if len(feeds) < 2 or len(set(feeds)) != len(feeds) or not all(FEED_CODE.match(f) for f in feeds):
            raise UsageError(f"--feeds needs at least two distinct category codes: {args.feeds}")
        if not DAY_TEXT.match(args.start):
            raise UsageError(f"--start is not a day: {args.start}")
        try:
            start = dt.date.fromisoformat(args.start)
        except ValueError:
            raise UsageError(f"--start is not a day: {args.start}") from None
        if start.weekday() != 0:
            raise UsageError(f"--start must be a Monday: {args.start}")
        end = start + dt.timedelta(days=7 * args.weeks - 1)
        turn = (start.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        if turn < start + dt.timedelta(days=21) or turn > end - dt.timedelta(days=12):
            raise UsageError(f"the range {start.isoformat()} to {end.isoformat()} cannot hold the planted "
                             f"cases around {turn.isoformat()}")
        if os.path.exists(args.out) and (not os.path.isdir(args.out) or (
                os.listdir(args.out) and not os.path.isfile(os.path.join(args.out, "corpus.json")))):
            raise UsageError(f"--out is neither empty nor an earlier corpus: {args.out}")
        corpus = Corpus(args.seed, args.weeks, start, feeds, args.daily, args.topics)
        corpus.generate()
        corpus.assign_ids()
        for paper in corpus.papers:
            corpus.finish(paper)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    command = (f"python dev/make_corpus.py --seed {args.seed} --out <DIR> --weeks {args.weeks} "
               f"--start {args.start} --feeds {args.feeds} --daily {args.daily} --topics {args.topics}")
    papers, versions, days = write(args.out, corpus, command)
    print(f"wrote {args.out}: {papers} papers, {versions} versions, {days} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
