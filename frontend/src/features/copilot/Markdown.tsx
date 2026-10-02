/**
 * A deliberately small Markdown renderer for Copilot replies.
 *
 * Builds React elements directly — never HTML strings — so model output can
 * never inject markup. Supports what the Copilot actually writes: paragraphs,
 * **bold**, _italic_, `code`, bullet lists, fenced code, and pipe tables.
 */
import type { ReactNode } from 'react';

function inline(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  // Italics only at word boundaries, so merge tags like #first_name# survive.
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|(?<![\w#])_[^_\s][^_]*_(?![\w#]))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${keyPrefix}-${i++}`;
    if (token.startsWith('**')) parts.push(<strong key={key} className="font-semibold text-slate-900">{token.slice(2, -2)}</strong>);
    else if (token.startsWith('`')) parts.push(<code key={key} className="rounded bg-slate-100 px-1 py-0.5 text-[0.85em]">{token.slice(1, -1)}</code>);
    else parts.push(<em key={key}>{token.slice(1, -1)}</em>);
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

export default function Markdown({ text }: { text: string }) {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim().startsWith('```')) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith('```')) body.push(lines[i++]);
      i += 1;
      blocks.push(
        <pre key={key++} className="overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100">
          {body.join('\n')}
        </pre>,
      );
      continue;
    }
    if (line.trim().startsWith('|')) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(lines[i++]);
      const [head, ...rest] = rows;
      const body = rest.filter((r) => !/^\|?\s*:?-{2,}/.test(r.trim()));
      blocks.push(
        <div key={key++} className="my-2 overflow-x-auto rounded-lg ring-1 ring-slate-200">
          <table className="min-w-full divide-y divide-slate-200 text-xs">
            <thead className="bg-slate-50">
              <tr>
                {cells(head).map((c, j) => (
                  <th key={j} className="whitespace-nowrap px-3 py-1.5 text-left font-semibold text-slate-600">{c}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {body.map((r, k) => (
                <tr key={k}>
                  {cells(r).map((c, j) => (
                    <td key={j} className="whitespace-nowrap px-3 py-1.5 text-slate-700">{inline(c, `t${key}-${k}-${j}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^\s*[-*] /.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*] /, ''));
      blocks.push(
        <ul key={key++} className="my-1.5 list-disc space-y-0.5 pl-5">
          {items.map((item, j) => <li key={j}>{inline(item, `l${key}-${j}`)}</li>)}
        </ul>,
      );
      continue;
    }
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].trim().startsWith('|') &&
      !lines[i].trim().startsWith('```') &&
      !/^\s*[-*] /.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    blocks.push(
      <p key={key++} className="my-1.5 leading-relaxed">
        {para.flatMap((p, j) => (j ? [<br key={`br${j}`} />, ...inline(p, `p${key}-${j}`)] : inline(p, `p${key}-${j}`)))}
      </p>,
    );
  }
  return <div className="text-sm text-slate-700">{blocks}</div>;
}
