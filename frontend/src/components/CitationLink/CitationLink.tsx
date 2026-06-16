// Renders markdown with [n] markers resolved to clickable hyperlinks (§8.3).
// Resolves [n] -> source URL from the report's sources[].
import ReactMarkdown from "react-markdown";
import { Box, Link } from "@mui/material";
import type { Source } from "../../types";

interface Props {
  markdown: string;
  sources: Source[];
  onCitationClick?: (id: number) => void;
}

const CITE_RE = /\[(\d+)\]/g;

export default function CitationMarkdown({ markdown, sources, onCitationClick }: Props) {
  const byId = new Map(sources.map((s) => [s.id, s]));

  // Replace [n] with a custom token react-markdown leaves intact, then post-process via
  // a components override on text is complex; instead split paragraphs and inline links.
  const rendered = markdown.replace(CITE_RE, (_m, d) => {
    const id = Number(d);
    const src = byId.get(id);
    if (!src) return `[${id}]`;
    return `[\\[${id}\\]](${src.url} "${(src.title || src.url).replace(/"/g, "'")}")`;
  });

  return (
    <Box sx={{ "& a": { color: "primary.main", fontWeight: 600 }, "& table": { borderCollapse: "collapse" }, "& td,& th": { border: "1px solid #ccc", p: 0.5 } }}>
      <ReactMarkdown
        components={{
          a: ({ href, children }) => (
            <Link
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => {
                const m = String(children).match(/\d+/);
                if (m && onCitationClick) onCitationClick(Number(m[0]));
              }}
            >
              {children}
            </Link>
          ),
        }}
      >
        {rendered}
      </ReactMarkdown>
    </Box>
  );
}
