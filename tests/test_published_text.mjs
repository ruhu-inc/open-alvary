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
  const corpus = spec.map(([kind, locator, from, to, more = {}], order) => ({
    id: "n_" + locator,
    parent_id: more.parent ? "n_" + more.parent : null,
    kind,
    locator,
    order: more.order ?? order,
    start_byte: start(from) + Buffer.byteLength(blocks[from].slice(0, more.skip ?? 0)),
    end_byte: more.until === undefined ? end(to) : start(to) + more.until,
    label: more.label ?? null,
    heading: more.heading ?? null,
    cell_states: more.cell_states ?? null,
  }));
  return {
    text,
    nodes: sent(corpus),
    version: { id: version, language: "en", canonical_text: text, content_hash: "h", bytes: Buffer.byteLength(text) },
  };
}

// The corpus's nodes as the data path sends them (reader-1; api/reader.py is the code that does it, and
// tests/test_reader_projection.py holds it to this): in document order, with no parents; an indentation
// node with its depth and nothing else; some nodes not sent at all.
function sent(corpus) {
  const INDENTS = ["subsection", "paragraph", "subparagraph"];
  const byId = new Map(corpus.map((node) => [node.id, node]));
  const above = (node) => {
    const up = [];
    for (let at = byId.get(node.parent_id); at; at = byId.get(at.parent_id)) up.push(at);
    return up;
  };
  const own = (node) => node.kind === "paragraph" && ["opinion", "order"].includes(byId.get(node.parent_id)?.kind);
  const goes = (node) =>
    node.end_byte > node.start_byte &&
    !["definition", "quotation"].includes(node.kind) &&
    !own(node) &&
    !(node.kind === "contents_entry" && above(node).some((at) => at.kind === "contents"));
  return corpus
    .filter(goes)
    .sort(
      (a, b) =>
        a.start_byte - b.start_byte ||
        b.end_byte - a.end_byte ||
        above(a).length - above(b).length ||
        a.order - b.order ||
        (a.id < b.id ? -1 : 1),
    )
    .map((node) => {
      const { kind, start_byte, end_byte } = node;
      if (INDENTS.includes(kind)) {
        const depth = [node, ...above(node)].filter((at) => INDENTS.includes(at.kind) && !own(at)).length;
        return { kind, start_byte, end_byte, depth: Math.min(3, depth) };
      }
      const full = { id: node.id, kind, locator: node.locator, start_byte, end_byte };
      for (const name of ["label", "heading", "cell_states"]) if (node[name]) full[name] = node[name];
      return full;
    });
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
    assert.equal(nodesFor(version, nodes).length, nodes.length);
  });

  it("is refused where it does not describe these words", () => {
    const { version, nodes } = french_();
    const with_ = (change) => nodes.map((node, index) => (index === 3 ? { ...node, ...change } : node));
    // Past the end of the text, backwards, with no bytes, inside a character (É is two bytes), out of
    // the order they are sent in, or of a text of another length.
    assert.deepEqual(nodesFor(version, with_({ end_byte: byteLength(version.canonical_text) + 1 })), []);
    assert.deepEqual(nodesFor(version, with_({ start_byte: nodes[3].end_byte + 1 })), []);
    assert.deepEqual(nodesFor(version, with_({ end_byte: nodes[3].start_byte })), []);
    const inside = Buffer.byteLength(version.canonical_text.slice(0, version.canonical_text.indexOf("É"))) + 1;
    assert.deepEqual(nodesFor(version, with_({ end_byte: inside })), []);
    assert.deepEqual(nodesFor(version, [...nodes].reverse()), []);
    assert.deepEqual(nodesFor({ ...version, bytes: version.bytes + 1 }, nodes), []);
    assert.equal(nodesFor(version, with_({})).length, nodes.length);
  });
});

describe("a paragraph a reading took for a heading", () => {
  // As a machine reading left a treaty: each numbered paragraph read as a heading, so the structure
  // rules took its whole sentence for the heading of a division or an article.
  const sentence =
    "The United Nations, its property and assets wherever located and by whomsoever held, shall enjoy " +
    "immunity from every form of legal process except insofar as in any particular case it has expressly " +
    "waived its immunity. It is, however, understood that no waiver of immunity shall extend to any " +
    "measure of execution.";
  const long = "É".repeat(190);
  const { text, nodes } = fixture(
    [
      "CONVENTION ON AN INVENTED MATTER",
      "ARTICLE II: PROPERTY, FUNDS AND ASSETS",
      "SECTION 2. " + sentence,
      "Article 3 " + sentence.slice(0, 215),
      "Article 4 " + long,
      "Its words.",
    ],
    [
      ["document", "document", 0, 5],
      ["chapter", "chap_ii", 1, 5, { parent: "document", label: "ARTICLE II:", heading: "PROPERTY, FUNDS AND ASSETS" }],
      ["division", "chap_ii__dvs_2", 2, 2, { parent: "chap_ii", label: "SECTION 2.", heading: sentence }],
      ["article", "art_3", 3, 3, { parent: "chap_ii", label: "Article 3", heading: sentence.slice(0, 215) }],
      ["article", "art_4", 4, 5, { parent: "chap_ii", label: "Article 4", heading: long }],
    ],
  );
  const laid = layoutText(text, nodes);

  it("is set as text, keeps its anchor and is listed by its number alone", () => {
    assert.equal(joined(laid), text);
    const [, chapter, division, article, real] = blocksOf(laid);
    assert.equal(chapter.type, "heading");
    // Over 300 characters for a part, over 200 for a provision: not a heading.
    assert.deepEqual([division.type, division.id], ["paragraph", "chapter-ii-division-2"]);
    assert.deepEqual([article.type, article.id, article.lead], ["paragraph", "article-3", 0]);
    assert.deepEqual(
      laid.contents.map((entry) => entry.label),
      // A real heading stays a heading in the text; its entry is cut like any long entry.
      ["ARTICLE II: PROPERTY, FUNDS AND ASSETS", "SECTION 2.", "Article 3", ("Article 4 " + long).slice(0, 140) + "…"],
    );
  });

  it("leaves a long heading that is one: the longest real ones run to 199 characters", () => {
    const real = blocksOf(laid)[4];
    assert.deepEqual([real.type, real.id, real.text.length], ["heading", "article-4", 200]);
  });
});

describe("nodes as the data path sends them", () => {
  it("reads nesting from spans: a part and its only section with the same span, the part first", () => {
    // The corpus numbers siblings only, so the section (first of its part) has a lower order than the
    // part (second of the document). Sent by span, then by how deep, the part still comes first.
    const { text, nodes } = fixture(
      ["An Act", "PART II — THE REGISTER", "4. The register is kept by the Registrar.", "(a) in the form set out;"],
      [
        ["document", "document", 0, 3],
        ["part", "part_ii", 1, 3, { parent: "document", label: "PART II", heading: "THE REGISTER", order: 1 }],
        ["section", "sec_4", 1, 3, { parent: "part_ii", label: "4.", order: 0 }],
        ["paragraph", "sec_4__para_a", 3, 3, { parent: "sec_4", label: "(a)" }],
      ],
    );
    assert.deepEqual(nodes.map((node) => node.kind), ["document", "part", "section", "paragraph"]);
    const laid = layoutText(text, nodesFor({ canonical_text: text, bytes: Buffer.byteLength(text) }, nodes));
    assert.equal(joined(laid), text);
    const [, part, section, inner] = blocksOf(laid);
    // The part's heading takes the anchor; the section is a level below it and listed after it.
    assert.deepEqual([part.type, part.id, part.level], ["heading", "part-ii", 3]);
    assert.deepEqual(laid.contents.map((entry) => entry.label), ["PART II — THE REGISTER", "4."]);
    assert.deepEqual([section.type, section.depth, inner.depth], ["paragraph", 0, 1]);
  });

  it("does not let a node that opens part-way through a block hold that block", () => {
    const { text, nodes } = fixture(
      ["7. (1) The Minister may make rules.", "(2) Rules are laid before the House."],
      [
        ["section", "sec_7", 0, 1, { label: "7." }],
        ["subsection", "sec_7__subsec_1", 0, 0, { parent: "sec_7", label: "(1)", skip: 3 }],
        ["subsection", "sec_7__subsec_2", 1, 1, { parent: "sec_7", label: "(2)" }],
      ],
    );
    const [first, second] = blocksOf(layoutText(text, nodes));
    assert.deepEqual([first.depth, first.id, second.depth], [0, "section-7", 1]);
  });

  it("sets a judgment's numbered paragraphs flush, and what is inside one in one step", () => {
    const { text, nodes } = fixture(
      ["JUDGMENT", "[1] The appellant was convicted.", "(a) of theft; and", "[2] He appeals.", "ORDERS", "The appeal is dismissed."],
      [
        ["document", "document", 0, 5],
        ["opinion", "opinion_1", 0, 3, { parent: "document", label: "JUDGMENT" }],
        ["paragraph", "opinion_1__para_1", 1, 2, { parent: "opinion_1", label: "[1]" }],
        ["subparagraph", "opinion_1__para_1__a", 2, 2, { parent: "opinion_1__para_1", label: "(a)" }],
        ["paragraph", "opinion_1__para_2", 3, 3, { parent: "opinion_1", label: "[2]" }],
        ["order", "order_1", 4, 5, { parent: "document", heading: "ORDERS" }],
      ],
    );
    // The judgment's own paragraphs are not sent; the sub-paragraph is, one step in.
    assert.deepEqual(nodes.map((node) => [node.kind, node.depth ?? null]), [
      ["document", null],
      ["opinion", null],
      ["subparagraph", 1],
      ["order", null],
    ]);
    const laid = layoutText(text, nodes);
    assert.equal(joined(laid), text);
    assert.deepEqual(
      blocksOf(laid).map((piece) => [piece.type, piece.depth ?? null]),
      [["heading", null], ["paragraph", 0], ["paragraph", 1], ["paragraph", 0], ["heading", null], ["paragraph", 0]],
    );
    assert.deepEqual(laid.contents, []);
  });

  it("treats a kind it does not know as a container: never a heading, never listed", () => {
    const { text, nodes } = fixture(
      ["RECITALS", "Whereas the parties agree.", "Article 1: Purpose", "Its words."],
      [
        ["recital", "recital_1", 0, 1, { label: "RECITALS", heading: "RECITALS" }],
        ["article", "art_1", 2, 3, { label: "Article 1", heading: "Purpose" }],
      ],
    );
    const laid = layoutText(text, nodes);
    assert.deepEqual(blocksOf(laid).map((piece) => piece.type), ["paragraph", "paragraph", "heading", "paragraph"]);
    assert.deepEqual(laid.contents.map((entry) => entry.label), ["Article 1: Purpose"]);
  });

  it("leaves out a definition's node but keeps the depth of what is inside it, and cuts a long entry", () => {
    const long = "Interpretation of the words and expressions used in this Act and in every instrument made under it, " +
      "including regulations, rules, orders and notices of every kind";
    const { text, nodes } = fixture(
      [long, "2. In this Act—", "“register” means—", "(a) the register kept under section 4;"],
      [
        ["section", "sec_2", 0, 3, { label: "2.", heading: long }],
        ["definition", "sec_2__def_register", 2, 3, { parent: "sec_2" }],
        ["paragraph", "sec_2__def_register__para_a", 3, 3, { parent: "sec_2__def_register", label: "(a)" }],
      ],
    );
    assert.deepEqual(nodes.map((node) => node.kind), ["section", "paragraph"]);
    const laid = layoutText(text, nodes);
    assert.deepEqual(blocksOf(laid).map((piece) => piece.depth ?? null), [null, 0, 0, 1]);
    const [entry] = laid.contents;
    assert.ok(entry.label.length <= 141 && entry.label.endsWith("…") && entry.label.startsWith("2. Interpretation of the words"));
    assert.ok(!entry.label.slice(0, -1).endsWith(" "));
  });
});
