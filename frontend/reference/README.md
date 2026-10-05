# UI reference

Source: https://vocalremover.org/ — rendered DOM inspected on 2026-10-03.
Direct HTTP scraping received a Cloudflare challenge; the rendered page was
read through the browser. `vocalremover.html` records the landing fragment.
It is documentation, not an application entry point.

Measured styles:

| Element | Value |
| --- | --- |
| Background | `#17171e` |
| Navigation | `#1c1c26` |
| Text | `#eeeeee` |
| Secondary text | `#d8d8e2` |
| Purple accent / upload border | `#665dc3` |
| Green accent | `#00ff8e` |
| Border | `#262633` |
| Upload border | `2px`, radius `32px` |
| Heading | `calc(2rem + 1vw)`, weight `700` |
| Subtitle | `calc(1rem + .5vw)`, weight `300` |
| Font family | Source Sans Pro, sans-serif |

The React implementation uses Source Sans 3, a narrow desktop navigation with
one Remover tool, local SVG waveform artwork, and Indonesian copy adapted to
the actual local backend. It does not load the reference site's scripts,
images, tracking, or processing service. The decorative waveform is an
illustration; result players use the actual audio returned by the API.
