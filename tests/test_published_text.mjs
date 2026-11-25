// The record page's text layout (web/src/published-text.ts): slicing a published text by its
// structure's UTF-8 byte spans, its headings and anchors, and its contents.
//
//   node --test tests/test_published_text.mjs
//
// Node runs the TypeScript file as it is (types are stripped; Node 22.18 or later).
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  byteLength,
  chooseVersion,
  foldsAway,
  layoutText,
  nodesFor,
} from "../web/src/published-text.ts";

// A text as oa-text-1 writes it (blocks apart by a blank line, one line feed at the end) and nodes
// over its blocks. A node spans blocks [from, to]; `skip` characters of its first block come before it.
function fixture(blocks, spec, version = "ver_1") {
  const text = blocks.join("\n\n") + "\n";
  const start = (i) => Buffer.byteLength(blocks.slice(0, i).join("\n\n")) + (i ? 2 : 0);
  const end = (i) => start(i) + Buffer.byteLength(blocks[i]);
  const nodes = spec.map(([kind, locator, from, to, more = {}], order) => ({
    id: "n_" + locator,
    version_id: version,
    parent_id: more.parent ? "n_" + more.parent : null,
    kind,
    locator,
    order,
    start_byte: start(from) + Buffer.byteLength(blocks[from].slice(0, more.skip ?? 0)),
    end_byte: end(to),
    label: more.label ?? null,
    heading: more.heading ?? null,
    cell_states: more.cell_states ?? null,
  }));
  return { text, nodes, version: { id: version, language: "en", canonical_text: text, content_hash: "h" } };
}

const joined = (laid) => laid.pieces.map((piece) => piece.text).join("");
const blocksOf = (laid) => laid.pieces.filter((piece) => piece.type !== "gap");

// One language of a Rwandan law, as rw-law-1 structures it: the printed table of contents is one
// node holding its lines, a chapter's label keeps its colon, an article's does not.
const FRENCH = [
  "LOI N° 000/2099 DU 01/01/2099 RÉGISSANT UN REGISTRE INVENTÉ",
  "TABLE DES MATIÈRES",
  "CHAPITRE PREMIER: DISPOSITIONS GÉNÉRALES",
  "Article premier: Objet de la présente loi",
  "Article 2: Définitions des termes",
  "CHAPITRE PREMIER: DISPOSITIONS GÉNÉRALES",
  "Article premier: Objet de la présente loi",
  "La présente loi régit un registre inventé.",
  "Article 2: Définitions des termes",
  "Aux fins de la présente loi, les termes suivants ont les significations suivantes:",
  "1° «registre»: le registre inventé tenu pour cet essai;",
  "CHAPITRE II: DU REGISTRE",
  "Section première: Tenue du registre",
  "Article 3: Inscriptions au registre",
  "Toute inscription est portée au registre le jour même où elle est reçue.",
  "Fait à Kigali, le 01/01/2099",
];
const french_ = () =>
  fixture(FRENCH, [
    ["document", "document", 0, 15],
    ["contents", "contents", 1, 4, { parent: "document", heading: "TABLE DES MATIÈRES" }],
    ["chapter", "chap_premier", 5, 10, { parent: "document", label: "CHAPITRE PREMIER:", heading: "DISPOSITIONS GÉNÉRALES" }],
    ["article", "art_1", 6, 7, { parent: "chap_premier", label: "Article premier", heading: "Objet de la présente loi" }],
    ["article", "art_2", 8, 10, { parent: "chap_premier", label: "Article 2", heading: "Définitions des termes" }],
    ["chapter", "chap_ii", 11, 14, { parent: "document", label: "CHAPITRE II:", heading: "DU REGISTRE" }],
    ["division", "chap_ii__dvs_premi_re", 12, 14, { parent: "chap_ii", label: "Section première:", heading: "Tenue du registre" }],
    ["article", "art_3", 13, 14, { parent: "chap_ii__dvs_premi_re", label: "Article 3", heading: "Inscriptions au registre" }],
    ["notice", "notice_1", 15, 15, { parent: "document" }],
  ]);

describe("slicing by UTF-8 bytes", () => {
  it("finds a node by its byte span, where bytes and characters differ", () => {
    // ’ is three bytes and é two: every span after them is further on in bytes than in characters.
    assert.equal(byteLength("by’amagambo"), 13);
    const blocks = ["ITEGEKO RIGENGA IGITABO CY’IGERAGEZA", "Ingingo ya 2: Ibisobanuro by’amagambo", "Iri tegeko rigenga igitabo cy’igerageza."];
    const { text, nodes } = fixture(blocks, [
      ["article", "art_2", 1, 2, { label: "Ingingo ya 2", heading: "Ibisobanuro by’amagambo" }],
    ]);
    assert.ok(nodes[0].start_byte > text.indexOf("Ingingo ya 2"));
    const laid = layoutText(text, nodes);
    assert.deepEqual(
      blocksOf(laid).map((piece) => [piece.type, piece.id, piece.text]),
      [
        ["paragraph", null, blocks[0]],
        ["heading", "article-2", blocks[1]],
        ["paragraph", null, blocks[2]],
      ],
    );
    // Read by characters, the same span would start two characters late and head nothing.
    const whole = french_();
    const article = whole.nodes.find((node) => node.locator === "art_2");
    const words = FRENCH.slice(8, 11).join("\n\n");
    assert.ok(article.end_byte - article.start_byte > words.length);
    assert.equal(
      new TextDecoder().decode(new TextEncoder().encode(whole.text).slice(article.start_byte, article.end_byte)),
      words,
    );
  });
});

describe("the text laid out from its structure", () => {
  it("keeps every character once, in order", () => {
    const { text, nodes } = french_();
    assert.equal(joined(layoutText(text, nodes)), text);
    // Also where the text does not follow oa-text-1: single line feeds, blank lines at both ends.
    const odd = "\n\nTitle\nSecond line\n\n\n\nArticle 1: É\n\tBody with a tab\n\n";
    assert.equal(joined(layoutText(odd, [])), odd);
  });

  it("sets chapters, divisions and articles as headings with anchors, and the rest as paragraphs", () => {
    const { text, nodes } = french_();
    const laid = blocksOf(layoutText(text, nodes));
    const headings = laid.filter((piece) => piece.type === "heading" && !piece.printed);
    assert.deepEqual(
      headings.map((piece) => [piece.text, piece.id, piece.rank, piece.level]),
      [
        ["CHAPITRE PREMIER: DISPOSITIONS GÉNÉRALES", "chapter-premier", 2, 3],
        ["Article premier: Objet de la présente loi", "article-1", 4, 4],
        ["Article 2: Définitions des termes", "article-2", 4, 4],
        ["CHAPITRE II: DU REGISTRE", "chapter-ii", 2, 3],
        ["Section première: Tenue du registre", "chapter-ii-division-premi-re", 3, 4],
        ["Article 3: Inscriptions au registre", "article-3", 4, 5],
      ],
    );
    const paragraphs = laid.filter((piece) => piece.type === "paragraph" && !piece.printed);
    assert.deepEqual(
      paragraphs.map((piece) => piece.text),
      [FRENCH[0], FRENCH[7], FRENCH[9], FRENCH[10], FRENCH[14], FRENCH[15]],
    );
    assert.ok(paragraphs.every((piece) => piece.id === null && piece.depth === 0 && piece.lead === 0));
  });

  it("lists chapters, divisions and articles as printed, each linked to its heading", () => {
    const { text, nodes } = french_();
    assert.deepEqual(layoutText(text, nodes).contents, [
      { id: "chapter-premier", label: "CHAPITRE PREMIER: DISPOSITIONS GÉNÉRALES", part: true },
      { id: "article-1", label: "Article premier: Objet de la présente loi", part: false },
      { id: "article-2", label: "Article 2: Définitions des termes", part: false },
      { id: "chapter-ii", label: "CHAPITRE II: DU REGISTRE", part: true },
      { id: "chapter-ii-division-premi-re", label: "Section première: Tenue du registre", part: true },
      { id: "article-3", label: "Article 3: Inscriptions au registre", part: false },
    ]);
  });

  it("shows the law's own printed table of contents in place, and lists none of it", () => {
    const { text, nodes } = french_();
    const laid = layoutText(text, nodes);
    const printed = blocksOf(laid).filter((piece) => piece.printed);
    assert.deepEqual(
      printed.map((piece) => [piece.type, piece.text, piece.id]),
      [
        ["heading", "TABLE DES MATIÈRES", null],
        ["paragraph", FRENCH[2], null],
        ["paragraph", FRENCH[3], null],
        ["paragraph", FRENCH[4], null],
      ],
    );
    assert.ok(laid.contents.every((entry) => entry.label !== "TABLE DES MATIÈRES"));
    // A family that gives each printed line a node of its own (contents_entry) lists none of them.
    const entries = fixture(
      ["ARRANGEMENT OF SECTIONS", "1. Short title", "Short title", "1. This Act may be cited as the Test Act."],
      [
        ["contents", "contents", 0, 1, { heading: "ARRANGEMENT OF SECTIONS" }],
        ["contents_entry", "contents__entry_1", 1, 1, { parent: "contents", label: "1.", heading: "Short title" }],
        ["section", "sec_1", 2, 3, { label: "1.", heading: "Short title" }],
      ],
    );
    const act = layoutText(entries.text, entries.nodes);
    assert.deepEqual(act.contents, [{ id: "section-1", label: "1. Short title", part: false }]);
    assert.deepEqual(
      blocksOf(act).map((piece) => [piece.type, piece.printed, piece.id]),
      [
        ["heading", true, null],
        ["paragraph", true, null],
        ["heading", false, "section-1"],
        ["paragraph", false, null],
      ],
    );
  });

  it("reads a section headed by its margin note, with its number set apart and its parts indented", () => {
    const blocks = [
      "Interpretation",
      "2. (1) In this Act, a word has the meaning this section gives it.",
      "(a) “register” means the register kept under this Act;",
      "(2) Subsection (1) applies unless the context requires otherwise.",
    ];
    const { text, nodes } = fixture(blocks, [
      ["section", "sec_2", 0, 3, { label: "2.", heading: "Interpretation" }],
      ["subsection", "sec_2__subsec_1", 1, 2, { parent: "sec_2", label: "(1)", skip: 3 }],
      ["paragraph", "sec_2__subsec_1__para_a", 2, 2, { parent: "sec_2__subsec_1", label: "(a)" }],
      ["subsection", "sec_2__subsec_2", 3, 3, { parent: "sec_2", label: "(2)" }],
    ]);
    const laid = layoutText(text, nodes);
    assert.equal(joined(laid), text);
    assert.deepEqual(laid.contents, [{ id: "section-2", label: "2. Interpretation", part: false }]);
    assert.deepEqual(
      blocksOf(laid).map((piece) => [piece.type, piece.id, piece.depth ?? null]),
      [
        ["heading", "section-2", null],
        // "2. (1) …" is one printed paragraph, though subsection (1) opens inside it.
        ["paragraph", null, 0],
        ["paragraph", null, 2],
        ["paragraph", null, 1],
      ],
    );
    // A section printed with no heading keeps its anchor on its paragraph, its number set apart.
    const bare = fixture(["3. This Act binds the State."], [["section", "sec_3", 0, 0, { label: "3." }]]);
    const [only] = blocksOf(layoutText(bare.text, bare.nodes));
    assert.deepEqual([only.type, only.id, only.text.slice(0, only.lead)], ["paragraph", "section-3", "3. "]);
    assert.deepEqual(layoutText(bare.text, bare.nodes).contents, [{ id: "section-3", label: "3.", part: false }]);
  });

  it("reads a number alone as a heading, with its heading on the next line", () => {
    const { text, nodes } = fixture(
      ["ARTICLE 1", "DEFINITIONS", "In this Treaty, “Party” means a State bound by it.", "ANNEX I", "ARTICLE 1"],
      [
        ["article", "art_1", 0, 2, { label: "ARTICLE 1", heading: "DEFINITIONS" }],
        ["annex", "annex_i", 3, 4, { label: "ANNEX I" }],
        ["article", "annex_i__art_1", 4, 4, { parent: "annex_i", label: "ARTICLE 1" }],
      ],
    );
    const laid = layoutText(text, nodes);
    assert.deepEqual(
      blocksOf(laid).map((piece) => [piece.type, piece.id]),
      [
        ["heading", "article-1"],
        ["heading", null],
        ["paragraph", null],
        ["heading", "annex-i"],
        ["heading", "annex-i-article-1"],
      ],
    );
    assert.deepEqual(
      laid.contents.map((entry) => entry.label),
      ["ARTICLE 1 DEFINITIONS", "ANNEX I", "ARTICLE 1"],
    );
  });

  it("gives two parts with one locator their own anchors", () => {
    const { text, nodes } = fixture(["Article 5: First", "Article 5: Second"], [
      ["article", "art_5", 0, 0, { label: "Article 5", heading: "First" }],
      ["article", "art_5", 1, 1, { label: "Article 5", heading: "Second" }],
    ]);
    nodes[1].id = "n_art_5_again";
    assert.deepEqual(
      layoutText(text, nodes).contents.map((entry) => entry.id),
      ["article-5", "article-5-2"],
    );
  });

  it("reads a table's block as rows and cells, with the cells a reviewer marked", () => {
    const { text, nodes } = fixture(["First Schedule", "Fees", "Item\tFee\nEntry\t"], [
      ["schedule", "sch_1", 0, 2, { label: "First Schedule" }],
      ["table", "sch_1__table_1", 1, 2, { parent: "sch_1", heading: "Fees", cell_states: { "1,1": "illegible" } }],
    ]);
    const laid = layoutText(text, nodes);
    assert.equal(joined(laid), text);
    const [heading, caption, table] = blocksOf(laid);
    assert.deepEqual([heading.type, heading.id, caption.type, table.type], ["heading", "schedule-1", "paragraph", "table"]);
    assert.deepEqual(table.rows, [["Item", "Fee"], ["Entry", ""]]);
    assert.deepEqual(table.states, { "1,1": "illegible" });
  });
});

describe("tables and long printed contents, as real Rwandan laws have them", () => {
  it("reads a block of tabbed rows as a table though no node says so", () => {
    // rw-law-1 gives a table no node of its own: it is a block of the article that holds it.
    const grid = "Code\tMeaning\n310\tFiling number\n320\t";
    const { text, nodes } = fixture(["Article 2: Codes", "The codes are these:", grid, "They apply from today."], [
      ["article", "art_2", 0, 3, { label: "Article 2", heading: "Codes" }],
    ]);
    const laid = layoutText(text, nodes);
    assert.equal(joined(laid), text);
    assert.deepEqual(
      blocksOf(laid).map((piece) => piece.type),
      ["heading", "paragraph", "table", "paragraph"],
    );
    assert.deepEqual(blocksOf(laid)[2].rows, [["Code", "Meaning"], ["310", "Filing number"], ["320", ""]]);
    // A stray tab in running words is no table: the rows must all have as many cells.
    const stray = fixture(["Article 3: Words", "one\ttwo\nthree"], [
      ["article", "art_3", 0, 1, { label: "Article 3", heading: "Words" }],
    ]);
    assert.deepEqual(
      blocksOf(layoutText(stray.text, stray.nodes)).map((piece) => piece.type),
      ["heading", "paragraph"],
    );
  });

  it("folds a long printed table of contents away only where the Contents lists it again", () => {
    assert.equal(foldsAway(231, 206), true); // the Constitution of Rwanda: 230 lines under "TABLE OF CONTENTS"
    assert.equal(foldsAway(8, 7), false); // a short one stays open
    assert.equal(foldsAway(13, 7), true);
    assert.equal(foldsAway(12, 7), false);
    // A text whose structure put its whole body under "contents" is never hidden: nothing lists it.
    assert.equal(foldsAway(900, 0), false);
    assert.equal(foldsAway(900, 20), false);
  });
});

describe("a version's structure", () => {
  it("is none where the version has no nodes, and the text is then shown plain", () => {
    const { version, nodes } = french_();
    assert.deepEqual(nodesFor(version, []), []);
    assert.deepEqual(nodesFor(version, undefined), []);
    assert.deepEqual(nodesFor({ ...version, id: "ver_other" }, nodes), []);
    assert.equal(nodesFor(version, nodes).length, nodes.length);
  });

  it("is refused where it does not describe these words", () => {
    const { version, nodes } = french_();
    const with_ = (change) => nodes.map((node, index) => (index === 3 ? { ...node, ...change } : node));
    // Past the end of the text, backwards, inside a character (É is two bytes), or of another text.
    assert.deepEqual(nodesFor(version, with_({ end_byte: byteLength(version.canonical_text) + 1 })), []);
    assert.deepEqual(nodesFor(version, with_({ start_byte: 90, end_byte: 80 })), []);
    const inside = Buffer.byteLength(version.canonical_text.slice(0, version.canonical_text.indexOf("É"))) + 1;
    assert.deepEqual(nodesFor(version, with_({ start_byte: inside })), []);
    assert.deepEqual(nodesFor(version, with_({ source_hash: "another" })), []);
    assert.equal(nodesFor(version, with_({ source_hash: "h" })).length, nodes.length);
  });
});

describe("the text opened first", () => {
  const text = (id, language, more = {}) => ({ id, language, canonical_text: "", content_hash: id, ...more });
  const served = [text("ver_en", "en"), text("ver_fr", "fr"), text("ver_rw", "rw")];

  it("is the record's own language, else the first served", () => {
    assert.equal(chooseVersion(served, "rw").id, "ver_rw");
    assert.equal(chooseVersion(served, "sw").id, "ver_en");
    assert.equal(chooseVersion(served, undefined).id, "ver_en");
    assert.equal(chooseVersion([], "en"), undefined);
  });

  it("is the one a link asks for, by language or by version", () => {
    assert.equal(chooseVersion(served, "rw", { language: "fr" }).id, "ver_fr");
    assert.equal(chooseVersion(served, "rw", { language: "FR" }).id, "ver_fr");
    assert.equal(chooseVersion(served, "rw", { version: "ver_en", language: "fr" }).id, "ver_en");
    // A language or version the record does not have changes nothing.
    assert.equal(chooseVersion(served, "rw", { language: "pt", version: "ver_gone" }).id, "ver_rw");
  });

  it("is, within a language, the latest text that no later reading corrects", () => {
    const several = [
      text("ver_2016", "en", { effective_from: "2016-01-01" }),
      text("ver_2020_misread", "en", { effective_from: "2020-06-01" }),
      text("ver_2020", "en", { effective_from: "2020-06-01", prior_version_id: "ver_2020_misread" }),
      text("ver_fr", "fr"),
    ];
    assert.equal(chooseVersion(several, "en").id, "ver_2020");
    assert.equal(chooseVersion(several, "en", { version: "ver_2016" }).id, "ver_2016");
  });
});
