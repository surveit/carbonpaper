// The review queue's client-side pager.
//
// The server ships every queued card, but inside a <template>: parsed, never
// laid out. This module holds those cards in one collection and hands the page
// one page of them at a time, so layout costs one page rather than one queue.
//
// That collection is a card's CURRENT state, not the DOM: a card re-rendered
// after a decision is swapped in here too, so paging away and back shows the
// decision rather than the undecided card the page was loaded with.
(function (global) {
  "use strict";

  // Cards per page. A card runs to several hundred pixels, so this is already a
  // long scroll; the reviewer's place in the whole queue is carried by each
  // card's own absolute "Row N of TOTAL", never renumbered per page.
  var QUEUE_PAGE_SIZE = 25;

  // `cards` is [{fingerprint, card}] in review order; `card` is opaque here.
  function createQueuePager(cards, pageSize) {
    if (!(pageSize > 0)) {
      throw new Error("queue pager: pageSize must be a positive number, got " + pageSize);
    }
    var entries = cards.slice();

    function pageCount() {
      return Math.max(1, Math.ceil(entries.length / pageSize));
    }

    function clampPage(index) {
      if (!isFinite(index)) return 0;
      return Math.min(Math.max(Math.floor(index), 0), pageCount() - 1);
    }

    return {
      total: function () { return entries.length; },
      pageCount: pageCount,
      clampPage: clampPage,

      cardsOnPage: function (index) {
        var start = clampPage(index) * pageSize;
        return entries.slice(start, start + pageSize).map(function (entry) {
          return entry.card;
        });
      },

      // Loudly, because a swap that misses here is invisible until the reviewer
      // pages back and reads a decided row as undecided.
      replaceCard: function (fingerprint, card) {
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].fingerprint === fingerprint) {
            entries[i] = { fingerprint: fingerprint, card: card };
            return i;
          }
        }
        throw new Error("queue pager: no queued card with fingerprint " + fingerprint);
      },

      // The row numbers are the ones the cards themselves show: absolute over
      // the whole queue, so the readout and the card headers cannot disagree.
      describePage: function (index) {
        var page = clampPage(index);
        var first = entries.length === 0 ? 0 : page * pageSize + 1;
        var last = Math.min((page + 1) * pageSize, entries.length);
        return {
          page: page + 1,
          pages: pageCount(),
          first: first,
          last: last,
          total: entries.length,
          label: "Page " + (page + 1) + " of " + pageCount(),
          range: "rows " + first + "–" + last + " of " + entries.length,
        };
      },
    };
  }

  global.createQueuePager = createQueuePager;
  global.QUEUE_PAGE_SIZE = QUEUE_PAGE_SIZE;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      createQueuePager: createQueuePager,
      QUEUE_PAGE_SIZE: QUEUE_PAGE_SIZE,
    };
  }
})(typeof window !== "undefined" ? window : globalThis);
