/**
 * A deliberately small, streaming-safe markdown renderer for the copilot panel.
 *
 * WHY THIS EXISTS
 *
 * The panel had exactly one mutation site for assistant text —
 * `body.textContent += delta` — and deltas are raw token chunks, so **partial
 * markdown is guaranteed**. A bullet list arrives as "- Un", "ique pat",
 * "ients\n- Enc"…, and any renderer that tries to reconcile a half-parsed
 * structure incrementally will eventually render garbage and then correct
 * itself on screen.
 *
 * The way out is to never reconcile. Every bubble keeps its `raw` accumulator,
 * and each animation frame clears the node and re-parses the whole buffer. An
 * answer is a few kilobytes; a full re-render per frame is cheaper than being
 * clever, and it makes partial markdown trivially correct — an unterminated
 * emphasis renders as a literal asterisk, an unterminated fence renders as an
 * open code block, and the next frame fixes both.
 *
 * WHAT IS DELIBERATELY MISSING
 *
 * No links, no images, no HTML passthrough. Model output is untrusted input by
 * definition; every one of those is an injection surface, and none is worth
 * having in a 380px side panel. Links in particular: a copilot that can render
 * `[click here](javascript:…)` into a page that already holds the user's
 * Superset session is a bad trade for prettier prose.
 *
 * TABLES ARE SUPPORTED, and were not always. They were grouped with the list
 * above and dropped, which was a mistake — not because tables are safe (they
 * are: a `<table>` built from `createElement` carries no more risk than a `<p>`)
 * but because the reasoning conflated "injection surface" with "too wide for the
 * panel". The second is a layout problem, and it is solved by letting the table
 * scroll inside its own container.
 *
 * What made it worth fixing is that the model emits them regardless. "What are
 * the actual numbers in that chart" is an inherently tabular question, and the
 * answer arrived on screen as
 *
 *     | Subcounty | Facilities | |---|---| | Njeru T.C. | 9 | | Wakisi | 6 | …
 *
 * — one unreadable run of pipes, in the panel's flagship interaction. Telling
 * the model not to use tables would have been the other fix, and it is worse:
 * it degrades the answer to avoid a rendering gap.
 *
 * ZERO innerHTML. Every node is built with `document.createElement` and
 * `textContent`, exactly as the rest of the panel is, and `test_end_to_end.py`
 * asserts that no HTML sink appears in either file.
 *
 * Exposed as `window.__lifedataMarkdown` so Playwright can drive it directly —
 * including over every prefix of a sample answer, which is the streaming-safety
 * test that matters.
 */

(function () {
  "use strict";

  /** Block-level patterns, checked in order. */
  var HEADING = /^(#{1,3})\s+(.*)$/;
  var BULLET = /^\s*[-*+]\s+(.*)$/;
  var ORDERED = /^\s*(\d{1,3})[.)]\s+(.*)$/;
  var QUOTE = /^>\s?(.*)$/;
  var FENCE = /^\s*```(.*)$/;
  //: A table row is any line fenced by pipes. Whether it IS a table depends on
  //: the line below it being a separator — see `separatorAlignments`.
  var TABLE_ROW = /^\s*\|(.+)\|\s*$/;
  //: One separator cell: `---`, `:---`, `---:` or `:---:`.
  var TABLE_SEPARATOR_CELL = /^:?-+:?$/;

  /**
   * The trimmed cells of a pipe-delimited line, or null if it is not one.
   * @param {string} line
   * @returns {Array<string>|null}
   */
  function tableCells(line) {
    var match = TABLE_ROW.exec(line);
    if (!match) {
      return null;
    }
    return match[1].split("|").map(function (cell) {
      return cell.trim();
    });
  }

  /**
   * Whether `line` is a table separator (`|---|:--:|`).
   *
   * This is what distinguishes a real table from a sentence containing pipes,
   * and it is also the streaming guard: while only the header has arrived there
   * is no separator beneath it yet, so the header renders as an ordinary
   * paragraph and is promoted to a table on the frame after the separator lands.
   *
   * Alignment markers are PARSED but not applied. Per-column alignment would
   * mean `style="text-align: right"` on a cell, and this renderer's strongest
   * property is that nothing it produces carries an attribute at all —
   * `test_no_rendered_element_ever_carries_an_attribute` asserts exactly that,
   * by walking the live DOM. Right-aligned numbers are not worth being the first
   * attribute in the tree: once one is allowed the test has to be weakened, and
   * then it no longer catches the attribute that would matter.
   *
   * @param {string} line
   * @returns {boolean}
   */
  function isSeparatorRow(line) {
    var cells = tableCells(line);
    if (!cells || !cells.length) {
      return false;
    }
    for (var i = 0; i < cells.length; i++) {
      if (!TABLE_SEPARATOR_CELL.test(cells[i])) {
        return false;
      }
    }
    return true;
  }

  /**
   * @param {string} tag
   * @param {string} [text]
   * @returns {HTMLElement}
   */
  function make(tag, text) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null && text !== "") {
      node.textContent = String(text);
    }
    return node;
  }

  /**
   * Split a line into inline runs. One pass, leftmost-match-wins, and an
   * unterminated marker is emitted as a literal — which is what makes a
   * half-streamed line render sensibly rather than as an empty element.
   *
   * @param {string} text
   * @returns {Array<Node>}
   */
  function inline(text) {
    var out = [];
    var buffer = "";
    var index = 0;

    function flush() {
      if (buffer) {
        out.push(document.createTextNode(buffer));
        buffer = "";
      }
    }

    while (index < text.length) {
      var rest = text.slice(index);
      var matched = null;

      // Order matters: `**` must be tried before `*`, or bold renders as two
      // empty italics wrapped around the text.
      if (rest.charAt(0) === "`") {
        matched = closed(rest, "`", "`");
        if (matched) {
          flush();
          out.push(make("code", matched.inner));
        }
      } else if (rest.slice(0, 2) === "**") {
        matched = closed(rest, "**", "**");
        if (matched) {
          flush();
          appendInline(out, make("strong"), matched.inner);
        }
      } else if (rest.slice(0, 2) === "__") {
        matched = closed(rest, "__", "__");
        if (matched) {
          flush();
          appendInline(out, make("strong"), matched.inner);
        }
      } else if (rest.charAt(0) === "*" || rest.charAt(0) === "_") {
        var marker = rest.charAt(0);
        matched = closed(rest, marker, marker);
        if (matched) {
          flush();
          appendInline(out, make("em"), matched.inner);
        }
      }

      if (matched) {
        index += matched.length;
      } else {
        buffer += text.charAt(index);
        index += 1;
      }
    }
    flush();
    return out;
  }

  /** Nest one more level of inline parsing inside an element. */
  function appendInline(out, element, text) {
    inline(text).forEach(function (child) {
      element.appendChild(child);
    });
    out.push(element);
  }

  /**
   * Find a closing marker, or null when the run is still being streamed.
   * @returns {{inner: string, length: number}|null}
   */
  function closed(rest, open, close) {
    var from = open.length;
    var end = rest.indexOf(close, from);
    if (end === -1 || end === from) {
      // Unterminated, or empty (`**`): treat the marker as literal text.
      return null;
    }
    return { inner: rest.slice(from, end), length: end + close.length };
  }

  /**
   * Render markdown into a fresh DocumentFragment.
   *
   * @param {string} source
   * @returns {DocumentFragment}
   */
  function render(source) {
    var fragment = document.createDocumentFragment();
    var lines = String(source == null ? "" : source).split(/\r?\n/);

    var paragraph = [];
    var list = null;
    var listTag = null;
    var quote = null;
    var fence = null;
    var table = null;

    function endParagraph() {
      if (!paragraph.length) {
        return;
      }
      var p = make("p");
      inline(paragraph.join("\n")).forEach(function (child) {
        p.appendChild(child);
      });
      fragment.appendChild(p);
      paragraph = [];
    }

    function endList() {
      if (list) {
        fragment.appendChild(list);
        list = null;
        listTag = null;
      }
    }

    function endQuote() {
      if (quote) {
        fragment.appendChild(quote);
        quote = null;
      }
    }

    function endTable() {
      if (table) {
        fragment.appendChild(table.node);
        table = null;
      }
    }

    /** One cell. No attributes — see `isSeparatorRow`. */
    function cell(tag, text) {
      var node = make(tag);
      inline(text).forEach(function (child) {
        node.appendChild(child);
      });
      return node;
    }

    function startTable(headerCells) {
      // No wrapper element and no class: the table scrolls itself, via
      // `.ld-msg__body table { display: block; overflow-x: auto }` in panel.css.
      // A wrapper would need a class to be styleable, and a class is an
      // attribute — see `isSeparatorRow` for why that line is held.
      var node = make("table");
      var head = make("thead");
      var headRow = make("tr");
      headerCells.forEach(function (text) {
        headRow.appendChild(cell("th", text));
      });
      head.appendChild(headRow);
      node.appendChild(head);
      var body = make("tbody");
      node.appendChild(body);
      return { node: node, body: body };
    }

    function appendRow(cells) {
      var row = make("tr");
      cells.forEach(function (text) {
        row.appendChild(cell("td", text));
      });
      table.body.appendChild(row);
    }

    function endBlocks() {
      endParagraph();
      endList();
      endQuote();
      endTable();
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      // Inside a fence, everything is literal until the closing ``` — including
      // things that look like headings and bullets.
      if (fence) {
        var closing = FENCE.exec(line);
        if (closing) {
          fragment.appendChild(fence.node);
          fence = null;
        } else {
          fence.lines.push(line);
          fence.code.textContent = fence.lines.join("\n");
        }
        continue;
      }

      var fenceStart = FENCE.exec(line);
      if (fenceStart) {
        endBlocks();
        var pre = make("pre");
        var code = make("code");
        pre.appendChild(code);
        // The node is attached as soon as the fence OPENS, so a code block
        // still being streamed is visible rather than appearing all at once
        // when the closing fence arrives.
        fence = { node: pre, code: code, lines: [] };
        fragment.appendChild(pre);
        continue;
      }

      if (!line.trim()) {
        endBlocks();
        continue;
      }

      var heading = HEADING.exec(line);
      if (heading) {
        endBlocks();
        var level = Math.min(heading[1].length, 3);
        var h = make("h" + (level + 2)); // h3..h5: the panel has its own h2.
        inline(heading[2]).forEach(function (child) {
          h.appendChild(child);
        });
        fragment.appendChild(h);
        continue;
      }

      var quoted = QUOTE.exec(line);
      if (quoted) {
        endParagraph();
        endList();
        if (!quote) {
          quote = make("blockquote");
        }
        var qp = make("p");
        inline(quoted[1]).forEach(function (child) {
          qp.appendChild(child);
        });
        quote.appendChild(qp);
        continue;
      }
      endQuote();

      var cells = tableCells(line);
      if (cells) {
        if (table) {
          appendRow(cells);
          continue;
        }
        // Only the line BELOW decides whether this is a table. Look ahead once;
        // if there is no separator there, fall through and let the line be
        // ordinary text — mid-stream that is exactly right, because the
        // separator has simply not arrived yet and the next frame re-parses
        // from scratch anyway.
        if (i + 1 < lines.length && isSeparatorRow(lines[i + 1])) {
          endParagraph();
          endList();
          table = startTable(cells);
          i++; // The separator is consumed, never rendered.
          continue;
        }
      }
      endTable();

      var bullet = BULLET.exec(line);
      var ordered = ORDERED.exec(line);
      if (bullet || ordered) {
        endParagraph();
        var wanted = bullet ? "ul" : "ol";
        if (listTag !== wanted) {
          endList();
          list = make(wanted);
          listTag = wanted;
        }
        var li = make("li");
        inline(bullet ? bullet[1] : ordered[2]).forEach(function (child) {
          li.appendChild(child);
        });
        list.appendChild(li);
        continue;
      }
      endList();

      paragraph.push(line);
    }

    endBlocks();
    return fragment;
  }

  /**
   * Replace a node's children with rendered markdown.
   * @param {HTMLElement} node
   * @param {string} source
   */
  function renderInto(node, source) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
    node.appendChild(render(source));
  }

  window.__lifedataMarkdown = { render: render, renderInto: renderInto };
})();
