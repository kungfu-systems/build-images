# latex-pdf-builder

Pinned LaTeX PDF builder for Kungfu publication-artifact repositories.

This image extends `node24-pnpm`, which extends the Kungfu `base-linux` image.
The inherited toolchain provides the non-root `kungfu` user, Node.js 24, and
`pnpm@11.7.0`. This layer adds `latexmk`, `biber`, Ghostscript, and a practical
TeX Live package set for paper-style PDF builds.

## Usage

Publication repositories should keep their build command in `package.json` and
run it through pnpm:

```bash
docker run --rm -v "$PWD:/work" -w /work \
  ghcr.io/kungfu-systems/build-images/latex-pdf-builder:v1.2.0-alpha.0 \
  pnpm run pdf
```

A typical script is:

```json
{
  "scripts": {
    "pdf": "latexmk -pdf -outdir=_build paper/main.tex"
  }
}
```

Consumers that require reproducibility should pin the published digest recorded
by Buildchain release evidence instead of relying only on the mutable tag name.
