// Reading mode for a reader-1 descriptor (RDR-02). The DOM is built only from typed nodes:
// text is rendered as React text, never as HTML, and DOM order is canonical reading order.

import type { ReactElement } from "react";

export type ReaderCell = {
  row: number;
  column: number;
  row_span: number;
  column_span: number;
  node_id: string;
  header: boolean;
  state: "present" | "empty" | "unavailable";
};

export type ReaderNode = {
  node_id: string;
  anchor_id: string | null;
  parent_id: string | null;
  kind:
    | "notice"
    | "paragraph"
    | "table"
    | "table_cell"
    | "footnote_group"
    | "footnote";
  start: number | null;
  end: number | null;
  text: string | null;
  state: "present" | "empty" | "unavailable";
  references: { start: number; end: number; footnote_node_id: string }[];
  rows: number | null;
  columns: number | null;
  cells: ReaderCell[];
};

export type ReaderDescriptor = {
  schema_version: "reader-1";
  version_id: string;
  content_hash: string;
  representation_id: string;
  representation_revision: number;
  modes: { reading: string; page_layout: string; original: string };
  limitations: string[];
  nodes: ReaderNode[];
};

const LIMITATIONS: Record<string, string> = {
  geometry_unavailable:
    "Page layout is unavailable: no reviewed page geometry exists for this version.",
  original_not_eligible:
    "The original document is not available for viewing here.",
  synthetic_fixture: "Synthetic test material — not legislation.",
  cell_unavailable:
    "Some table cells could not be read from the source and are marked as unavailable.",
};

const encoder = new TextEncoder();
const decoder = new TextDecoder();

// Split text at UTF-8 byte ranges (the projection's addressing) into JS string pieces.
export function splitByBytes(
  text: string,
  ranges: { start: number; end: number }[],
): { text: string; range?: number }[] {
  const bytes = encoder.encode(text);
  const pieces: { text: string; range?: number }[] = [];
  let cursor = 0;
  ranges.forEach((range, index) => {
    if (range.start < cursor || range.end > bytes.length) return;
    if (range.start > cursor)
      pieces.push({ text: decoder.decode(bytes.slice(cursor, range.start)) });
    pieces.push({
      text: decoder.decode(bytes.slice(range.start, range.end)),
      range: index,
    });
    cursor = range.end;
  });
  if (cursor < bytes.length)
    pieces.push({ text: decoder.decode(bytes.slice(cursor)) });
  return pieces;
}

function anchorFor(node: ReaderNode | undefined): string | undefined {
  return node?.anchor_id ?? node?.node_id;
}

export function ReaderView({ descriptor }: { descriptor: ReaderDescriptor }) {
  const byId = new Map(descriptor.nodes.map((n) => [n.node_id, n]));
  const children = new Map<string | null, ReaderNode[]>();
  for (const node of descriptor.nodes) {
    children.set(node.parent_id, [
      ...(children.get(node.parent_id) ?? []),
      node,
    ]);
  }

  const Text = ({ node }: { node: ReaderNode }) => {
    if (node.text === null) return null;
    return (
      <>
        {splitByBytes(node.text, node.references).map((piece, i) => {
          if (piece.range === undefined)
            return <span key={i}>{piece.text}</span>;
          const target = byId.get(
            node.references[piece.range].footnote_node_id,
          );
          return (
            <a
              key={i}
              href={"#" + anchorFor(target)}
              className="reader-noteref"
              aria-label={"Footnote " + piece.text}
            >
              {piece.text}
            </a>
          );
        })}
      </>
    );
  };

  const Table = ({ node }: { node: ReaderNode }) => (
    <div className="reader-table" role="region" aria-label="Table" tabIndex={0}>
      <table id={anchorFor(node)}>
        <tbody>
          {Array.from({ length: node.rows ?? 0 }, (_, row) => (
            <tr key={row}>
              {node.cells
                .filter((cell) => cell.row === row)
                .map((cell) => {
                  const content = byId.get(cell.node_id);
                  const props = {
                    id: anchorFor(content),
                    rowSpan: cell.row_span,
                    colSpan: cell.column_span,
                    dir: "auto" as const,
                  };
                  const body =
                    cell.state === "unavailable" ? (
                      <span className="reader-uncertain">
                        [unavailable in source]
                      </span>
                    ) : content ? (
                      <Text node={content} />
                    ) : null;
                  return cell.header ? (
                    <th
                      key={cell.node_id}
                      {...props}
                      scope={cell.row === 0 ? "col" : "row"}
                    >
                      {body}
                    </th>
                  ) : (
                    <td key={cell.node_id} {...props}>
                      {body}
                    </td>
                  );
                })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  const Node = ({
    node,
    index,
  }: {
    node: ReaderNode;
    index: number;
  }): ReactElement | null => {
    const nested = (children.get(node.node_id) ?? []).map((child, i) => (
      <Node key={child.node_id} node={child} index={i} />
    ));
    switch (node.kind) {
      case "notice":
        return (
          <section
            className="reader-notice"
            id={anchorFor(node)}
            aria-label={"Notice " + (index + 1)}
          >
            {nested}
          </section>
        );
      case "paragraph":
        return (
          <p id={anchorFor(node)} dir="auto">
            <Text node={node} />
          </p>
        );
      case "table":
        return <Table node={node} />;
      case "footnote_group":
        return (
          <aside
            className="reader-footnotes"
            id={anchorFor(node)}
            aria-label="Footnotes"
          >
            {nested}
          </aside>
        );
      case "footnote":
        return (
          <p id={anchorFor(node)} dir="auto">
            <Text node={node} />
          </p>
        );
      default:
        return null; // table cells render inside their table
    }
  };

  return (
    <article className="reader" aria-label="Document text">
      <ul className="reader-limitations">
        {descriptor.limitations.map((code) => (
          <li key={code}>{LIMITATIONS[code] ?? code}</li>
        ))}
      </ul>
      <p className="reader-version">
        Version {descriptor.version_id} · representation revision{" "}
        {descriptor.representation_revision}
      </p>
      {(children.get(null) ?? []).map((node, i) => (
        <Node key={node.node_id} node={node} index={i} />
      ))}
    </article>
  );
}
