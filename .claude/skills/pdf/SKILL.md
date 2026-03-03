---
name: pdf
description: Convert a markdown file to a branded PDF with the clawbolt.ai logo. Use when the user wants to generate a PDF, share a document, or create a polished version of a markdown file.
argument-hint: <markdown-file>
allowed-tools: Read, Write, Edit, Glob, Bash, Grep
---

Convert the markdown file at `$ARGUMENTS` to a polished, branded PDF using weasyprint.

## Steps

1. **Read the target markdown file.** If `$ARGUMENTS` is empty, ask which file to convert.

2. **Locate the logo.** Look for `assets/clawbolt_text.png` in the repo root. If the markdown file
   does not already reference the logo, insert `![clawbolt.ai](assets/clawbolt_text.png)` as the
   first line (before the title).

3. **Set up the Python environment.** Install dependencies into a temporary venv:
   ```
   uv venv /tmp/pdfenv
   uv pip install --python /tmp/pdfenv/bin/python markdown weasyprint
   ```
   Also ensure system libraries are available:
   ```
   apt-get update -qq && apt-get install -y -qq libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev libcairo2 libglib2.0-0
   ```

4. **Write a Python conversion script** to `/tmp/md_to_pdf.py` that:
   - Reads the markdown file
   - Embeds the PNG logo as a base64 data URI so weasyprint can render it inline
   - Converts markdown to HTML using the `markdown` library with extensions:
     `tables`, `fenced_code`, `toc`, `smarty`
   - Wraps the HTML in a styled document with:
     - **Brand colors**: dark navy `#1a2332`, accent orange `#e8872e`
     - **Typography**: Helvetica Neue / Arial sans-serif, 10.5pt body
     - **Page setup**: letter size, 0.9in margins
     - **Headers**: h1 with orange bottom border, h2 with light gray bottom border
     - **Tables**: dark navy header row (`#1a2332` background, white text),
       alternating row striping
     - **Code**: inline code in `#c7254e` with gray background, code blocks with
       orange left border
     - **Links**: orange (`#e8872e`), no underline
     - **Blockquotes**: orange left border with light orange background
     - **HR**: orange line
     - **Logo**: max-width 240px at the top
     - **Page footer**: centered page number
     - **Running header** (pages 2+): "clawbolt.ai | {document title}" in italic gray,
       right-aligned (suppress on first page)
   - Outputs the PDF to the same directory as the source file, with a `.pdf` extension

5. **Run the script**: `/tmp/pdfenv/bin/python /tmp/md_to_pdf.py`

6. **Report the result**: Tell the user the output path and file size.

## Important notes

- If weasyprint system libs are already installed, skip the apt-get step.
- If the venv already exists at `/tmp/pdfenv` with the right packages, skip venv creation.
- The logo should be embedded as base64 in the HTML so the PDF is self-contained.
- Do NOT modify the source markdown file's content beyond adding the logo reference.
- Output the PDF alongside the source file (same directory, `.pdf` extension).
