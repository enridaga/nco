#!/usr/bin/env python3
"""Build the NCO documentation site.

Term descriptions are OWL Manchester syntax frames produced by ROBOT (OWLAPI),
hyperlinked and laid out as a static HTML page.

Usage:
    python3 build/build.py [--robot path/to/robot.jar]

Reads  build/metadata.json and <versionDir>/ontology.ttl
Writes <versionDir>/ontology.omn, <versionDir>/index.html,
       <versionDir>/index-en.html and index.html (latest, at the site root).
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import rdflib
from jinja2 import Environment, FileSystemLoader
from rdflib import OWL, RDF, RDFS, Namespace, URIRef

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
DCT = Namespace("http://purl.org/dc/terms/")

FRAME_KINDS = ("Class", "ObjectProperty", "DataProperty", "AnnotationProperty",
               "Individual", "Datatype", "DisjointClasses", "EquivalentClasses",
               "DisjointProperties", "EquivalentProperties", "SameIndividual",
               "DifferentIndividuals", "Rule")
KEYWORDS = {"some", "only", "value", "min", "max", "exactly", "and", "or",
            "not", "inverse", "that", "Self", "o"}
KIND_LABEL = {"Class": "Classes", "ObjectProperty": "Object properties",
              "DataProperty": "Data properties", "Individual": "Named individuals",
              "AnnotationProperty": "Annotation properties"}


# --------------------------------------------------------------------------
# Manchester syntax
# --------------------------------------------------------------------------

def to_manchester(ttl: Path, prefix: str, ns: str, robot: str) -> str:
    """Convert to Manchester syntax with ROBOT. owl:imports are stripped before
    conversion (so the build works offline) and re-added as Import: lines."""
    g = rdflib.Graph().parse(ttl)
    imports = sorted(str(o) for o in g.objects(None, OWL.imports))
    g.remove((None, OWL.imports, None))
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "in.ttl", Path(tmp) / "out.omn"
        g.serialize(src, format="turtle")
        cmd = ["java", "-jar", robot, "convert", "-i", str(src),
               "--add-prefix", f"{prefix}: {ns}", "-o", str(out)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        omn = out.read_text()
    imp = "".join(f"Import: <{i}>\n" for i in imports)
    # Insert imports right after the Ontology: header (and its version IRI).
    omn = re.sub(r"(^Ontology: <[^>]+>\n(?:<[^>]+>\n)?)", lambda m: m.group(1) + imp,
                 omn, count=1, flags=re.M)
    return omn


def split_frames(omn: str):
    """Split a Manchester document into (kind, subject, body_lines) frames.
    Tracks string literals so that multi-line annotation values are not
    mistaken for frame or section headers."""
    frames, cur, in_str = [], None, False
    head = re.compile(r"^(%s):\s*(.*)$" % "|".join(FRAME_KINDS))
    for line in omn.splitlines():
        m = None if in_str else head.match(line)
        if m:
            cur = [m.group(1), m.group(2).strip(), []]
            frames.append(cur)
        elif cur is not None:
            cur[2].append((line, in_str))
        # update string state (count unescaped quotes)
        in_str ^= (len(re.findall(r'(?<!\\)"', line)) % 2 == 1)
    return frames


def logical_sections(body):
    """Drop the frame-level Annotations: section and blank padding; keep
    every logical section (SubClassOf:, Domain:, Characteristics:, ...)."""
    out, skip = [], False
    sec = re.compile(r"^ {4}([A-Za-z]+):\s*(.*)$")
    for line, in_str in body:
        m = None if in_str else sec.match(line)
        if m:
            skip = m.group(1) == "Annotations"
        if not skip and line.strip():
            out.append(line.rstrip())
    return out


TOKEN = re.compile(r"""
    (?P<str>"(?:[^"\\]|\\.)*"(?:@[\w-]+|\^\^\S+)?)   # literal
  | (?P<iri><[^>\s]+>)                              # full IRI
  | (?P<section>^[ ]*[A-Z][A-Za-z]+:(?=\s|$))       # frame / section keyword
  | (?P<pname>[A-Za-z][\w-]*:[A-Za-z_][\w\-.]*[\w-]|[A-Za-z][\w-]*:[A-Za-z_])  # prefixed name
  | (?P<word>[A-Za-z]+)                             # keyword / bare word
""", re.X | re.M)


class Linker:
    def __init__(self, prefixes, ns, local_terms, labels):
        self.prefixes, self.ns = prefixes, ns
        self.local, self.labels = local_terms, labels

    def expand(self, pname):
        p, l = pname.split(":", 1)
        return self.prefixes.get(p, "") + l if p in self.prefixes else None

    def link(self, iri, text):
        t = html.escape(text)
        if iri and iri.startswith(self.ns) and iri[len(self.ns):] in self.local:
            local = iri[len(self.ns):]
            lab = html.escape(self.labels.get(iri, local))
            return f'<a class="t" href="#{local}" title="{lab}">{t}</a>'
        if iri:
            return f'<a class="x" href="{html.escape(iri)}" title="{html.escape(iri)}">{t}</a>'
        return t

    def render(self, text):
        out, pos = [], 0
        for m in TOKEN.finditer(text):
            out.append(html.escape(text[pos:m.start()]))
            g, v = m.lastgroup, m.group()
            if g == "str":
                out.append(f'<span class="lit">{html.escape(v)}</span>')
            elif g == "iri":
                out.append(self.link(v[1:-1], v))
            elif g == "section":
                ws = v[: len(v) - len(v.lstrip())]
                out.append(f'{ws}<span class="sec">{html.escape(v.strip())}</span>')
            elif g == "pname":
                out.append(self.link(self.expand(v), v))
            elif v in KEYWORDS:
                out.append(f'<span class="kw">{v}</span>')
            else:
                out.append(html.escape(v))
            pos = m.end()
        out.append(html.escape(text[pos:]))
        return "".join(out)


def dedent(lines):
    """Remove the 4-space frame indentation so blocks start at column 0."""
    return "\n".join(l[4:] if l.startswith("    ") else l for l in lines)


# --------------------------------------------------------------------------
# Ontology metadata (rdflib)
# --------------------------------------------------------------------------

def pick(g, s, p, lang="en"):
    vals = list(g.objects(s, p))
    pref = [v for v in vals if getattr(v, "language", None) in (lang, None)]
    return [str(v) for v in (pref or vals)]


def prose(text, linker):
    """Comment text → HTML paragraphs; `code` spans; nco:X mentions linked."""
    paras = []
    for para in re.split(r"\n\s*\n", text.strip()):
        parts = re.split(r"(`[^`]+`)", para)
        buf = []
        for part in parts:
            if part.startswith("`") and part.endswith("`") and len(part) > 1:
                buf.append(f"<code>{html.escape(part[1:-1])}</code>")
            else:
                esc = html.escape(part)
                esc = re.sub(r"\b%s:([A-Za-z_][\w]*)" % re.escape(linker.prefix_name),
                             lambda m: linker.link(linker.ns + m.group(1), m.group(0)), esc)
                buf.append(esc.replace("\n", "<br>"))
        paras.append(f"<p>{''.join(buf)}</p>")
    return "\n".join(paras)


def tree(children, roots, labels, ns, seen=None):
    seen = seen or set()
    items = []
    for c in sorted(roots, key=lambda x: labels.get(x, x).lower()):
        local = c[len(ns):]
        sub = ""
        if c not in seen and children.get(c):
            sub = tree(children, children[c], labels, ns, seen | {c})
        items.append(f'<li><a href="#{local}">{html.escape(labels.get(c, local))}</a>{sub}</li>')
    return f"<ul>{''.join(items)}</ul>"


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot", default=os.environ.get("ROBOT_JAR", "robot.jar"))
    args = ap.parse_args()

    meta = json.loads((BUILD / "metadata.json").read_text())
    vdir = ROOT / meta["versionDir"]
    ttl = vdir / "ontology.ttl"
    g = rdflib.Graph().parse(ttl)

    onto = next(g.subjects(RDF.type, OWL.Ontology))
    ns = str(onto) + "#"
    prefix = meta["prefix"]

    omn = to_manchester(ttl, prefix, ns, args.robot)
    (vdir / "ontology.omn").write_text(omn)
    prefixes = dict(re.findall(r"^Prefix: (\w*): <([^>]+)>", omn, re.M))

    # Local terms, keyed by local name
    kinds = [(OWL.Class, "Class"), (OWL.ObjectProperty, "ObjectProperty"),
             (OWL.DatatypeProperty, "DataProperty"),
             (OWL.AnnotationProperty, "AnnotationProperty"),
             (OWL.NamedIndividual, "Individual")]
    labels, local_terms = {}, set()
    for s in set(g.subjects(RDF.type, None)):
        if isinstance(s, URIRef) and str(s).startswith(ns) and len(str(s)) > len(ns):
            local_terms.add(str(s)[len(ns):])
            lab = pick(g, s, RDFS.label)
            labels[str(s)] = lab[0] if lab else str(s)[len(ns):]

    linker = Linker(prefixes, ns, local_terms, labels)
    linker.prefix_name = prefix

    # Frames → entries
    frames = split_frames(omn)
    refs = defaultdict(set)          # local name → set of (anchor, label)
    entries = defaultdict(list)
    general = []
    pname_re = re.compile(r"\b%s:([A-Za-z_][\w\-.]*[\w-]|[A-Za-z_])" % re.escape(prefix))

    def frame_local(subject):
        if subject.startswith(prefix + ":"):
            return subject.split(":", 1)[1]
        if subject.startswith("<" + ns):
            return subject[len(ns) + 1:-1]
        return None

    def add_general(kind, subject, lines):
        code = (f"{kind}: {subject}".rstrip() + "\n" + dedent(lines)).strip()
        general.append({"code": linker.render(code), "plain": code})
        for used in set(pname_re.findall(code)):
            refs[used].add(("general-axioms", "General axioms"))

    seen_anchor = set()
    for kind, subject, body in frames:
        if kind in KIND_LABEL or kind == "Datatype":
            local = frame_local(subject)
            if not local or kind == "Datatype":
                # External term: keep only if NCO adds axioms to it.
                lines = logical_sections(body)
                if lines:
                    add_general(kind, subject, lines)
                continue
            iri = ns + local
            anchor = local if local not in seen_anchor else f"{local}-{kind.lower()}"
            seen_anchor.add(anchor)
            lines = logical_sections(body)
            code = f"{kind}: {subject}\n" + dedent(lines) if lines else f"{kind}: {subject}"
            s = URIRef(iri)
            e = {
                "anchor": anchor, "local": local, "iri": iri, "kind": kind,
                "curie": f"{prefix}:{local}",
                "label": labels.get(iri, local),
                "comments": [prose(c, linker) for c in pick(g, s, RDFS.comment)],
                "definitions": [prose(c, linker) for c in pick(g, s, SKOS.definition)],
                "examples": [prose(c, linker) for c in pick(g, s, SKOS.example)],
                "seealso": [prose(c, linker) for c in pick(g, s, RDFS.seeAlso)],
                "deprecated": (s, OWL.deprecated, rdflib.Literal(True)) in g,
                "code": linker.render(code),
                "plain": code,
            }
            entries[kind].append(e)
            for used in set(pname_re.findall("\n".join(lines))) - {local}:
                refs[used].add((anchor, e["label"]))
        else:
            add_general(kind, subject, logical_sections(body))

    for kind in entries:
        entries[kind].sort(key=lambda e: e["label"].lower())
        for e in entries[kind]:
            e["refs"] = sorted(refs.get(e["local"], ()), key=lambda r: r[1].lower())

    # Hierarchies (named terms in this namespace only)
    def hierarchy(rel, typ):
        members = {str(s) for s in g.subjects(RDF.type, typ) if str(s).startswith(ns)}
        children, has_parent = defaultdict(set), set()
        for s, o in g.subject_objects(rel):
            s, o = str(s), str(o)
            if s in members and o in members and s != o:
                children[o].add(s)
                has_parent.add(s)
        return tree(children, members - has_parent, labels, ns) if members else ""

    class_tree = hierarchy(RDFS.subClassOf, OWL.Class)
    oprop_tree = hierarchy(RDFS.subPropertyOf, OWL.ObjectProperty)

    # Legacy LODE anchors (#d4eNNN) from the v1.4 docs → local names
    legacy = {}
    old = ROOT / "archive/1.4/docs/index.html"
    if old.exists():
        for aid, iri in re.findall(r'<div id="(d4e\d+)" class="entity">\s*<a name="([^"]+)"',
                                   old.read_text()):
            legacy[aid] = iri.rsplit("#", 1)[-1]

    # Ontology header
    o_title = (pick(g, onto, DCT.title) or pick(g, onto, RDFS.label) or [meta["title"]])[0]
    o_comments = pick(g, onto, RDFS.comment)
    abstract = prose(o_comments[0], linker) if o_comments else ""
    notes = [prose(c, linker) for c in o_comments[1:]]
    version_iri = next((str(v) for v in g.objects(onto, OWL.versionIRI)), None)
    imports = sorted(str(i) for i in g.objects(onto, OWL.imports))
    used_ns = sorted((p, n) for p, n in prefixes.items() if p)

    counts = {k: len(entries.get(k, [])) for k in KIND_LABEL}
    sections = [(k, KIND_LABEL[k]) for k in
                ("Class", "ObjectProperty", "DataProperty", "Individual", "AnnotationProperty")
                if entries.get(k)]

    env = Environment(loader=FileSystemLoader(BUILD), autoescape=False,
                      trim_blocks=True, lstrip_blocks=True)
    tpl = env.get_template("template.html")
    serialisations = [("Turtle", "ontology.ttl"), ("RDF/XML", "ontology.owl"),
                      ("N-Triples", "ontology.nt"), ("JSON-LD", "ontology.jsonld"),
                      ("Manchester", "ontology.omn")]

    def render(base, page_url):
        return tpl.render(
            meta=meta, title=o_title, onto=str(onto), ns=ns, prefix=prefix,
            version_iri=version_iri, imports=imports, abstract=abstract, notes=notes,
            entries=entries, sections=sections, general=general, counts=counts,
            class_tree=class_tree, oprop_tree=oprop_tree, namespaces=used_ns,
            serialisations=[(n, base + f) for n, f in serialisations if (vdir / f).exists()],
            webvowl=base + "webvowl/index.html#ontology" if (vdir / "webvowl").exists() else None,
            legacy=json.dumps(legacy, sort_keys=True), page_url=page_url,
            root=("../" if base == "" else ""),
            version_url=meta["siteBase"] + meta["versionDir"] + "/",
            latest_url=meta["siteBase"],
        )

    v = meta["versionDir"]
    page_v = render("", meta["siteBase"] + v + "/")
    (vdir / "index.html").write_text(page_v)
    (vdir / "index-en.html").write_text(page_v)   # served by .htaccess conneg
    (ROOT / "index.html").write_text(render(v + "/", meta["siteBase"]))
    print(f"Built {sum(counts.values())} terms "
          f"({', '.join(f'{n} {k}' for k, n in counts.items() if n)}), "
          f"{len(general)} general axioms, {len(legacy)} legacy anchors.")


if __name__ == "__main__":
    sys.exit(main())
