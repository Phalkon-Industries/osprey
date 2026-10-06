/* Project image viewer: thumbnails swap the main image, the main image
 * opens a full-screen view, arrows and swipe step through, Escape closes.
 */
(function () {
    "use strict";
    document.querySelectorAll("[data-gallery]").forEach(function (root) {
        var thumbs = Array.prototype.slice.call(root.querySelectorAll("[data-gallery-thumb]"));
        var main = root.querySelector("[data-gallery-main]");
        var caption = root.querySelector("[data-gallery-caption]");
        var box = root.querySelector("[data-gallery-lightbox]");
        var full = root.querySelector("[data-gallery-full]");
        var fullCaption = root.querySelector("[data-gallery-full-caption]");
        var current = 0;
        if (!thumbs.length || !main || !box) return;

        function show(i) {
            current = (i + thumbs.length) % thumbs.length;
            var t = thumbs[current];
            main.src = t.dataset.image;
            main.alt = t.dataset.caption || main.alt;
            if (caption) {
                caption.textContent = t.dataset.caption || "";
                caption.hidden = !t.dataset.caption;
            }
            thumbs.forEach(function (b, n) { b.classList.toggle("is-current", n === current); });
            if (!box.hidden) fillFull();
        }
        function fillFull() {
            var t = thumbs[current];
            full.src = t.dataset.full;
            full.alt = t.dataset.caption || "";
            fullCaption.textContent = t.dataset.caption || "";
        }
        function open() {
            fillFull();
            box.hidden = false;
            document.body.classList.add("gallery-open");
            var close = box.querySelector("[data-gallery-close]");
            if (close) close.focus();
        }
        function close() {
            box.hidden = true;
            document.body.classList.remove("gallery-open");
            root.querySelector("[data-gallery-open]").focus();
        }

        thumbs.forEach(function (t, n) {
            t.addEventListener("click", function () { show(n); });
        });
        root.querySelector("[data-gallery-open]").addEventListener("click", open);
        box.querySelector("[data-gallery-close]").addEventListener("click", close);
        var prev = box.querySelector("[data-gallery-prev]");
        var next = box.querySelector("[data-gallery-next]");
        if (prev) prev.addEventListener("click", function () { show(current - 1); });
        if (next) next.addEventListener("click", function () { show(current + 1); });
        box.addEventListener("click", function (e) { if (e.target === box) close(); });
        document.addEventListener("keydown", function (e) {
            if (box.hidden) return;
            if (e.key === "Escape") close();
            else if (e.key === "ArrowLeft") show(current - 1);
            else if (e.key === "ArrowRight") show(current + 1);
        });
        var startX = null;
        box.addEventListener("touchstart", function (e) { startX = e.touches[0].clientX; }, { passive: true });
        box.addEventListener("touchend", function (e) {
            if (startX === null) return;
            var dx = e.changedTouches[0].clientX - startX;
            if (Math.abs(dx) > 40) show(current + (dx < 0 ? 1 : -1));
            startX = null;
        });
    });
})();
