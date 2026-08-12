"""Corpus intake: licence policy, dedup, path construction, Range probing.

The licence rules are a compliance precondition, so they are tested as rules
rather than exercised in passing.  The network is mocked throughout — these
must run on a machine with no HF access and no corpus on disk.
"""

import io
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptxgym.delivery import corpus                                       # noqa: E402


# --------------------------------------------------------------------------- #
# licence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lic", ["cc-by-4.0", "cc-zero", "cc-by-sa-4.0",
                                 "mit-license", "other-pd", "etalab-2.0"])
def test_permissive_licences_pass(lic):
    v = corpus.licence_verdict(lic)
    assert v["ok"] and v["why"]


@pytest.mark.parametrize("lic,word", [
    ("cc-by-nd-4.0", "no-derivatives"),      # we damage the deck: a derivative
    ("cc-by-nc-4.0", "non-commercial"),
    ("cc-by-nc-nd-4.0", "non-commercial"),
    ("notspecified", "no licence stated"),
    ("other-closed", "closed"),
])
def test_restricted_licences_are_refused_with_a_reason(lic, word):
    v = corpus.licence_verdict(lic)
    assert not v["ok"]
    assert word in v["why"]


def test_an_unknown_licence_id_fails_closed():
    """The corpus has 36 ids today.  A 37th must not slip through on the
    strength of not matching any deny rule — the dataset card already claims
    every item is permissive, and 169 rows disprove it."""
    v = corpus.licence_verdict("cc-by-nc-nd-9.9-brand-new")
    assert not v["ok"] and "not in the reviewed list" in v["why"]


def test_share_alike_is_allowed_but_flagged():
    """Permitted, but the derivative inherits the licence — downstream has to
    know, so it cannot be silently lumped in with plain attribution."""
    v = corpus.licence_verdict("cc-by-sa-4.0")
    assert v["ok"] and v["share_alike"]
    assert corpus.licence_verdict("cc-by-4.0")["share_alike"] is False


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


def _row(name, lic="cc-by-4.0", ck="md5:aabbccdd", created="2019-01-01"):
    return {"filename": name, "license": lic, "checksum": ck,
            "created": created, "url": f"https://zenodo.org/{name}",
            "doi": "10.5281/zenodo.1", "size": 1234, "title": name,
            "updated": created}


def test_selection_drops_restricted_and_duplicates_with_reasons():
    rows = [_row("a.pptx"),
            _row("b.pptx", lic="cc-by-nc-4.0"),
            _row("c.pptx", ck="md5:aabbccdd"),          # same bytes as a.pptx
            _row("d.pptx", ck="md5:99887766")]
    sel = corpus.select(rows)
    assert [k["filename"] for k in sel["kept"]] == ["a.pptx", "d.pptx"]
    reasons = {d["filename"]: d["reason"] for d in sel["dropped"]}
    assert "non-commercial" in reasons["b.pptx"]
    assert "duplicate of a.pptx" in reasons["c.pptx"]
    assert sel["audit"]["in"] == 4 and sel["audit"]["out"] == 2


def test_path_follows_the_card_rule():
    """`pptx/<licence>/<year>/<md5>-<filename>`, and the checksum carries an
    `md5:` prefix that is not part of the name."""
    row = _row("UF DSI Symposium Talk.pptx", created="2018-06-01",
               ck="md5:49e3b6a509f6689aee174b2b780fa3f3")
    assert corpus.deck_path(row) == (
        "pptx/cc-by-4.0/2018/49e3b6a509f6689aee174b2b780fa3f3-"
        "UF DSI Symposium Talk.pptx")
    urls = corpus.deck_urls(row)
    assert urls[0].startswith(corpus.RESOLVE) and "%20" in urls[0]
    assert urls[1] == row["url"]        # Zenodo fallback: only 8,798 of the
                                        # 10,448 paths are listed as LFS


# --------------------------------------------------------------------------- #
# Range probing
# --------------------------------------------------------------------------- #


class FakeRanges:
    """Serves byte ranges out of an in-memory blob, like the CDN does."""

    def __init__(self, blob):
        self.blob = blob
        self.calls = 0

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls += 1
        rng = (headers or {}).get("Range", "").split("=")[-1]

        class R:
            status_code = 206
        if rng.startswith("-"):
            R.content = self.blob[-int(rng[1:]):]
        else:
            a, b = rng.split("-")
            R.content = self.blob[int(a):int(b) + 1]
        return R


def _zip_blob(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"x" * 64)
    return buf.getvalue()


def test_the_part_list_comes_from_two_range_requests():
    """Triaging the corpus by shape costs about a gigabyte this way instead of
    the 242 GB a download-everything pass would."""
    names = [f"ppt/slides/slide{i}.xml" for i in range(1, 13)] + [
        "ppt/media/image1.png", "ppt/media/image2.png",
        "ppt/charts/chart1.xml", "ppt/diagrams/data1.xml",
        "ppt/slides/_rels/slide1.xml.rels", "[Content_Types].xml"]
    sess = FakeRanges(_zip_blob(names))
    got = corpus.zip_manifest(sess, "http://x/deck.pptx")
    assert sorted(got) == sorted(names)
    assert sess.calls == 2

    shape = corpus.shape_from_manifest(got)
    assert shape["slides"] == 12          # the `_rels` entry is not a slide
    assert shape["media"] == 2 and shape["charts"] == 1 and shape["diagrams"] == 1


def test_slide_band_decides_in_band():
    lo, hi = corpus.SLIDE_BAND
    for n, want in ((lo - 1, False), (lo, True), (hi, True), (hi + 1, False)):
        sess = FakeRanges(_zip_blob(
            [f"ppt/slides/slide{i}.xml" for i in range(1, n + 1)] or ["a.xml"]))
        row = _row("x.pptx")
        assert corpus.probe(row, sess)["shape"]["in_band"] is want


def test_a_probe_failure_is_reported_not_raised():
    """One unreadable deck in ten thousand must not stop the sweep."""
    class Dead:
        def get(self, *a, **k):
            raise OSError("connection reset")
    out = corpus.probe(_row("x.pptx"), Dead())
    assert out["shape"] is None and "OSError" in out["probe_error"]


def test_a_non_zip_is_refused_clearly():
    sess = FakeRanges(b"not a zip at all" * 100)
    out = corpus.probe(_row("x.pptx"), sess)
    assert out["shape"] is None
    assert "central-directory" in out["probe_error"] or "ProbeError" in out["probe_error"]


# --------------------------------------------------------------------------- #
# metadata paging
# --------------------------------------------------------------------------- #


def test_the_split_is_asked_for_not_assumed(tmp_path):
    """It is `default/pptx`, not `default/train`.  Assuming the usual name got
    a 500 from the rows endpoint and "Not found." from first-rows — neither of
    which says what is wrong."""
    seen = {}

    class Sess:
        headers = {}

        def get(self, url, params=None, timeout=None, **k):
            class R:
                status_code = 200

                @staticmethod
                def raise_for_status():
                    pass
            if url.endswith("/splits"):
                R.json = lambda: {"splits": [{"dataset": corpus.REPO,
                                              "config": "default",
                                              "split": "pptx"}]}
                return R
            seen.update(params)
            n = min(params["length"], 3 - params["offset"])
            R.json = lambda: {"rows": [
                {"row": _row(f"f{params['offset'] + i}.pptx")}
                for i in range(max(0, n))]}
            return R

    rows = corpus._from_rows_api(Sess(), limit=3, pause=0)
    assert seen["split"] == "pptx" and seen["config"] == "default"
    assert len(rows) == 3


def test_the_index_is_cached_so_it_is_fetched_once(tmp_path, monkeypatch):
    cache = tmp_path / "index.jsonl"
    cache.write_text(json.dumps(_row("cached.pptx")) + "\n")

    def boom(*a, **k):
        raise AssertionError("should not have gone to the network")
    monkeypatch.setattr(corpus, "_session", boom)
    rows = corpus.fetch_metadata(cache)
    assert [r["filename"] for r in rows] == ["cached.pptx"]


# --------------------------------------------------------------------------- #
# triage
# --------------------------------------------------------------------------- #


@pytest.mark.corpus
def test_rich_slides_are_counted_not_summed():
    """`row["rich"]` was the value of an `or` chain, so a slide holding three
    charts counted as three rich slides — inflating the component worth 40 of
    the 100 points.  The invariant is that a slide can be rich at most once."""
    decks = sorted(Path("work").glob("deck*/source.pptx"))
    if not decks:
        pytest.skip("no corpus on disk")
    for d in decks[:4]:
        t = corpus.triage_deck(d)
        assert t["rich_slides"] <= t["usable_slides"] <= t["slides"]


@pytest.mark.corpus
def test_known_good_decks_are_not_rejected():
    """Ten decks that have already produced accepted tasks.  A filter that
    throws those away is wrong however sensible its weights look — the first
    version rejected two of them and hesitated over three more.

    Note what this cannot show: with no labelled bad decks, it fixes the floor
    but says nothing about what the filter lets through."""
    decks = sorted(Path("work").glob("deck*/source.pptx"))
    if len(decks) < 5:
        pytest.skip("no pilot corpus on disk")
    scored = [corpus.triage_deck(d) for d in decks]
    assert all(t["verdict"] != "reject" for t in scored), \
        [(Path(t["deck"]).parent.name, t["score"]) for t in scored
         if t["verdict"] == "reject"]


def test_rich_slides_are_counted_not_summed_on_a_frozen_deck(mini):
    """`row["rich"]` was the value of an `or` chain, so a slide holding three
    charts counted as three rich slides — inflating the component worth 40 of
    the 100 points.  The invariant is that a slide can be rich at most once,
    and it is arithmetic rather than a fact about any particular deck.

    What the frozen decks cannot show is the *other* half of the corpus twin
    — that ten decks which have already produced accepted tasks are not
    rejected by the filter.  That is a measurement of those ten decks, it
    needs their charts, tables, SmartArt and slide counts, and it stays
    `corpus`.
    """
    for name in sorted(mini.roots):
        t = corpus.triage_deck(mini.root(name) / "source.pptx")
        assert t["rich_slides"] <= t["usable_slides"] <= t["slides"], name


# --------------------------------------------------------------------------- #
# autoselect: the pure parts
# --------------------------------------------------------------------------- #


def test_choose_takes_top_scored_unused_font_covered_rows():
    """Selection is the strictness of the whole funnel: rows already spent on
    a batch, rows below the floor, rows naming fonts this machine cannot
    draw, and rows that never scored must all be invisible to it."""
    pool = [
        {"status": "scored", "score": 90, "checksum": "a"},
        {"status": "scored", "score": 95, "checksum": "b", "used_in": "b001"},
        {"status": "scored", "score": 80, "checksum": "c",
         "fonts_missing": ["wingbat display"]},
        {"status": "out_of_band", "checksum": "d"},
        {"status": "scored", "score": 40, "checksum": "e"},
        {"status": "scored", "score": 70, "checksum": "f"},
    ]
    got = corpus.choose(pool, 2)
    assert [r["checksum"] for r in got] == ["a", "f"]


def test_choose_breaks_ties_deterministically():
    """Equal scores must not reorder between runs — a rerun of the same pool
    has to pick the same decks, or the manifest stops meaning anything."""
    pool = [{"status": "scored", "score": 50, "checksum": c} for c in "ba"]
    assert [r["checksum"] for r in corpus.choose(pool, 2)] == ["a", "b"]


def test_advanced_focus_balances_capability_families():
    def row(key, score, **cap):
        return {"status": "scored", "score": score, "checksum": key,
                "capabilities": cap}
    pool = [
        row("anim", 90, animation_effects=12),
        row("anim2", 89, animation_effects=10),
        row("eq", 70, equations=1),
        row("chart", 80, charts=2),
        row("fx", 75, effects=4),
        row("plain", 99),
    ]
    got = corpus.choose(pool, 5, focus="advanced")
    assert [r["checksum"] for r in got[:4]] == ["anim", "eq", "chart", "fx"]
    assert got[4]["checksum"] == "anim2"
    assert "plain" not in {r["checksum"] for r in got}
    assert corpus.focus_assignments(got, "advanced") == {
        "anim": "animation", "eq": "equation", "chart": "chart",
        "fx": "effects", "anim2": "animation"}


def test_a_specific_focus_refuses_unlabelled_rows():
    pool = [
        {"status": "scored", "score": 99, "checksum": "plain"},
        {"status": "scored", "score": 60, "checksum": "eq",
         "capabilities": {"equations": 2}},
    ]
    assert [r["checksum"] for r in corpus.choose(pool, 2, focus="equation")] == ["eq"]
    with pytest.raises(ValueError, match="unknown focus"):
        corpus.choose(pool, 1, focus="video")


def _render_page(path, content=True):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (60, 40), "white")
    if content:
        ImageDraw.Draw(im).rectangle([10, 10, 40, 30], fill="blue")
    im.save(path)


def test_render_check_requires_a_complete_nonblank_render(tmp_path, monkeypatch):
    from pptxgym.office import render

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def complete(_pptx, out, prefix, **_kwargs):
        pages = [Path(out) / f"{prefix}-{i}.png" for i in (1, 2)]
        for page in pages:
            _render_page(page)
        return [str(p) for p in pages]

    monkeypatch.setattr(render, "render_pptx", complete)
    got = corpus.render_check(tmp_path / "deck.pptx", tmp_path, 2)
    assert got == {"ok": True, "status": "ok", "pages": 2}

    def short(*_args, **_kwargs):
        raise render.RenderFailed("rendered 1 of 2 slides")

    monkeypatch.setattr(render, "render_pptx", short)
    got = corpus.render_check(tmp_path / "deck.pptx", tmp_path, 2)
    assert not got["ok"] and got["status"] == "failed"
    assert "1 of 2" in got["error"]


def test_render_check_rejects_uniform_pages_and_missing_tools(tmp_path,
                                                              monkeypatch):
    from pptxgym.office import render

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def blank(_pptx, out, prefix, **_kwargs):
        page = Path(out) / f"{prefix}-1.png"
        _render_page(page, content=False)
        return [str(page)]

    monkeypatch.setattr(render, "render_pptx", blank)
    got = corpus.render_check(tmp_path / "deck.pptx", tmp_path, 1)
    assert not got["ok"] and got["status"] == "blank"

    monkeypatch.setattr("shutil.which", lambda _name: None)
    got = corpus.render_check(tmp_path / "deck.pptx", tmp_path, 1)
    assert not got["ok"] and got["status"] == "unavailable"


@pytest.mark.corpus
def test_scores_can_rank_what_the_old_clamps_tied():
    """12 of the first 30 staged decks tied at 100.0 under min(1, x/k)
    clamps — a score that cannot rank cannot pick the top 5% of a corpus.
    Saturation (x/(x+k)) never reaches the ceiling, so real decks should
    almost never collide exactly."""
    decks = sorted(Path("work").glob("deck*/source.pptx")) \
        + sorted(Path("workjobs").glob("deck*/source.pptx"))
    if len(decks) < 5:
        pytest.skip("no pilot corpus on disk")
    scores = [corpus.triage_deck(d)["score"] for d in decks]
    assert max(scores) < 100.0
    assert len(set(scores)) >= len(scores) - 1
