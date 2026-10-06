/* Images tab and README image insertion.
 *
 * Gallery images: drop or choose files, caption, reorder by drag or the
 * arrow buttons, remove. Every change saves immediately through the
 * image endpoints; the first tile is the cover. README images: the
 * Insert image button, paste, or drop into the README uploads the image
 * and inserts Markdown at the cursor. On a brand-new project the gallery
 * path saves the draft first; README images wait until the draft exists,
 * since the inserted Markdown would not survive the reload.
 */
(function () {
    "use strict";
    var editor = document.querySelector("[data-image-editor]");
    if (!editor) return;
    var form = editor.closest("form");
    var csrf = form && form.querySelector("input[name=csrfmiddlewaretoken]");
    var tiles = editor.querySelector("[data-image-tiles]");
    var readmeTiles = editor.querySelector("[data-readme-tiles]");
    var readmeList = editor.querySelector("[data-readme-image-list]");
    var errors = editor.querySelector("[data-image-errors]");
    var count = editor.querySelector("[data-image-count]");
    var input = editor.querySelector("[data-image-input]");
    var drop = editor.querySelector("[data-image-drop]");
    var max = parseInt(editor.dataset.max, 10) || 20;

    function url(template, id) { return template.replace(/\/0\//, "/" + id + "/"); }

    function post(target, body) {
        var fd = body instanceof FormData ? body : new FormData();
        if (!(body instanceof FormData) && body) {
            Object.keys(body).forEach(function (k) { fd.append(k, body[k]); });
        }
        return fetch(target, {
            method: "POST",
            body: fd,
            credentials: "same-origin",
            headers: { "X-CSRFToken": csrf ? csrf.value : "" },
        }).then(function (r) {
            return r.json().catch(function () { return { errors: ["Upload failed (" + r.status + ")."] }; });
        });
    }

    function showErrors(list) {
        if (!list || !list.length) { errors.hidden = true; errors.textContent = ""; return; }
        errors.hidden = false;
        errors.textContent = list.join(" ");
    }

    function setCount(n) { if (count) count.textContent = n + " of " + max + " images"; }

    function galleryTiles() { return Array.prototype.slice.call(tiles.querySelectorAll("[data-image-tile]")); }

    function announceCover() {
        var first = galleryTiles()[0];
        var src = first ? first.querySelector("[data-tile-thumb]").dataset.display : "";
        galleryTiles().forEach(function (t, i) {
            var label = t.querySelector("[data-cover-label]");
            if (label) label.hidden = i !== 0;
        });
        document.dispatchEvent(new CustomEvent("osprey:cover-changed", { detail: { src: src } }));
    }

    function saveOrder() {
        var ids = galleryTiles().map(function (t) { return t.dataset.id; }).join(",");
        post(editor.dataset.reorderUrl, { ids: ids });
        announceCover();
    }

    function makeTile(img, readme) {
        var li = document.createElement("li");
        li.className = "image-tile";
        li.dataset.imageTile = "";
        li.dataset.id = img.id;
        if (!readme) li.draggable = true;
        li.innerHTML =
            '<img alt="" data-tile-thumb>' +
            (readme ? "" : '<span class="image-tile-cover" data-cover-label hidden>Cover</span>') +
            '<input type="text" class="image-tile-caption" placeholder="Caption" maxlength="300" aria-label="Caption" data-tile-caption>' +
            '<span class="image-tile-actions">' +
            (readme ? "" : '<button type="button" class="ghost small" data-tile-earlier aria-label="Move earlier">&#8592;</button>' +
                '<button type="button" class="ghost small" data-tile-later aria-label="Move later">&#8594;</button>') +
            '<button type="button" class="ghost small" data-tile-remove>Remove</button></span>';
        var thumb = li.querySelector("[data-tile-thumb]");
        thumb.src = img.thumb;
        thumb.dataset.display = img.image;
        li.querySelector("[data-tile-caption]").value = img.caption || "";
        wire(li);
        return li;
    }

    function wire(li) {
        var cap = li.querySelector("[data-tile-caption]");
        var saved = cap.value;
        cap.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); cap.blur(); } });
        cap.addEventListener("change", function (e) { e.stopPropagation(); });
        cap.addEventListener("input", function (e) { e.stopPropagation(); });
        cap.addEventListener("blur", function () {
            if (cap.value === saved) return;
            saved = cap.value;
            post(url(editor.dataset.captionUrl, li.dataset.id), { caption: cap.value });
        });
        var earlier = li.querySelector("[data-tile-earlier]");
        var later = li.querySelector("[data-tile-later]");
        if (earlier) earlier.addEventListener("click", function () {
            if (li.previousElementSibling) { tiles.insertBefore(li, li.previousElementSibling); saveOrder(); }
        });
        if (later) later.addEventListener("click", function () {
            if (li.nextElementSibling) { tiles.insertBefore(li.nextElementSibling, li); saveOrder(); }
        });
        li.querySelector("[data-tile-remove]").addEventListener("click", function () {
            if (!window.confirm("Remove this image?")) return;
            post(url(editor.dataset.deleteUrl, li.dataset.id), {}).then(function (data) {
                var wasGallery = li.parentNode === tiles;
                li.remove();
                if (data && typeof data.count === "number") setCount(data.count);
                if (readmeList && readmeTiles && !readmeTiles.children.length) readmeList.hidden = true;
                if (wasGallery) announceCover();
            });
        });
        if (li.draggable) {
            li.addEventListener("dragstart", function (e) {
                li.classList.add("is-dragging");
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/x-osprey-tile", li.dataset.id);
            });
            li.addEventListener("dragend", function () { li.classList.remove("is-dragging"); saveOrder(); });
        }
    }

    tiles.addEventListener("dragover", function (e) {
        var dragging = tiles.querySelector(".is-dragging");
        if (!dragging) return;
        e.preventDefault();
        var after = galleryTiles().filter(function (t) { return t !== dragging; }).find(function (t) {
            var r = t.getBoundingClientRect();
            return e.clientY < r.top + r.height / 2 || (e.clientY < r.bottom && e.clientX < r.left + r.width / 2);
        });
        if (after) tiles.insertBefore(dragging, after);
        else tiles.appendChild(dragging);
    });

    editor.querySelectorAll("[data-image-tile]").forEach(wire);

    // A brand-new project has nowhere to put images yet. Adding one saves
    // the draft first (same as Save and continue), uploads into it, and
    // reopens the editor on this tab.
    function createDraftThenUpload(list) {
        // Check silently: checkValidity() fires "invalid", which the form
        // answers by switching to the tab of the first missing field.
        var valid = Array.prototype.every.call(form.elements, function (el) {
            return !el.willValidate || el.validity.valid;
        });
        if (!valid) {
            showErrors(["Fill in the required fields on Basics first, then add images."]);
            return Promise.resolve({ images: [] });
        }
        var fd = new FormData(form);
        fd.set("action", "save_continue");
        fd.set("next_tab", "images");
        showErrors([]);
        editor.classList.add("is-uploading");
        return fetch(form.getAttribute("action") || window.location.pathname, {
            method: "POST", body: fd, credentials: "same-origin",
        }).then(function (r) {
            var m = r.url.match(/\/projects\/([^/]+)\/edit\//);
            if (!r.redirected || !m) {
                editor.classList.remove("is-uploading");
                showErrors(["Fill in the required fields on Basics first, then add images."]);
                return { images: [] };
            }
            editor.dataset.uploadUrl = "/projects/" + m[1] + "/images/upload/";
            var up = new FormData();
            list.forEach(function (f) { up.append("files", f); });
            up.append("kind", "gallery");
            return post(editor.dataset.uploadUrl, up).then(function (data) {
                window.location.assign(r.url);
                return data;
            });
        });
    }

    function upload(files, kind) {
        var list = Array.prototype.slice.call(files || []).filter(function (f) { return f && f.size; });
        if (!list.length) return Promise.resolve({ images: [] });
        if (editor.hasAttribute("data-new-project") && !editor.dataset.uploadUrl) return createDraftThenUpload(list);
        var fd = new FormData();
        list.forEach(function (f) { fd.append("files", f); });
        fd.append("kind", kind);
        showErrors([]);
        editor.classList.add("is-uploading");
        return post(editor.dataset.uploadUrl, fd).then(function (data) {
            editor.classList.remove("is-uploading");
            showErrors(data.errors);
            (data.images || []).forEach(function (img) {
                if (kind === "readme") {
                    if (readmeTiles) { readmeTiles.appendChild(makeTile(img, true)); readmeList.hidden = false; }
                } else {
                    tiles.appendChild(makeTile(img, false));
                }
            });
            if (typeof data.count === "number") setCount(data.count);
            if (kind !== "readme") announceCover();
            return data;
        });
    }

    if (input) input.addEventListener("change", function (e) {
        e.stopPropagation();
        upload(input.files, "gallery").then(function () { input.value = ""; });
    });
    if (drop) {
        ["dragenter", "dragover"].forEach(function (t) {
            drop.addEventListener(t, function (e) {
                if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types, "Files") < 0) return;
                e.preventDefault();
                drop.classList.add("is-dragover");
            });
        });
        ["dragleave", "drop"].forEach(function (t) { drop.addEventListener(t, function () { drop.classList.remove("is-dragover"); }); });
        drop.addEventListener("drop", function (e) {
            if (!e.dataTransfer || !e.dataTransfer.files.length) return;
            e.preventDefault();
            upload(e.dataTransfer.files, "gallery");
        });
    }

    // ---- README images ----
    var readme = document.getElementById("id_readme");
    var readmeButton = document.querySelector("[data-readme-image-button]");
    var readmeInput = document.querySelector("[data-readme-image-input]");
    var readmeError = document.querySelector("[data-readme-image-error]");
    if (!readme) return;

    function insertAtCursor(text) {
        var start = readme.selectionStart || 0;
        var end = readme.selectionEnd || 0;
        var before = readme.value.slice(0, start);
        var pad = before && !/\n$/.test(before) ? "\n" : "";
        readme.value = before + pad + text + "\n" + readme.value.slice(end);
        var pos = (before + pad + text + "\n").length;
        readme.setSelectionRange(pos, pos);
        readme.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function uploadReadme(files) {
        if (readmeError) { readmeError.hidden = true; readmeError.textContent = ""; }
        if (editor.hasAttribute("data-new-project")) {
            if (readmeError) {
                readmeError.hidden = false;
                readmeError.textContent = "Save the draft first, then add images to the README.";
            }
            return Promise.resolve();
        }
        return upload(files, "readme").then(function (data) {
            (data.images || []).forEach(function (img) { insertAtCursor(img.markdown); });
            if (data.errors && data.errors.length && readmeError) {
                readmeError.hidden = false;
                readmeError.textContent = data.errors.join(" ");
            }
        });
    }

    if (readmeButton && readmeInput) {
        readmeButton.addEventListener("click", function () { readmeInput.click(); });
        readmeInput.addEventListener("change", function (e) {
            e.stopPropagation();
            uploadReadme(readmeInput.files).then(function () { readmeInput.value = ""; });
        });
    }
    readme.addEventListener("paste", function (e) {
        var files = e.clipboardData && e.clipboardData.files;
        if (!files || !files.length) return;
        e.preventDefault();
        uploadReadme(files);
    });
    readme.addEventListener("dragover", function (e) {
        if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, "Files") >= 0) e.preventDefault();
    });
    readme.addEventListener("drop", function (e) {
        if (!e.dataTransfer || !e.dataTransfer.files.length) return;
        e.preventDefault();
        uploadReadme(e.dataTransfer.files);
    });
})();
