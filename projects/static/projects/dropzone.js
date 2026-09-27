/* Drag-and-drop for file inputs.
 *
 * Any <input type="file" data-dropzone> gets wrapped in a drop target.
 * Dropping a file assigns it to the input (so the normal form submit and
 * the tab-completion logic see it) and fires `change`. The input stays
 * clickable for the usual file picker. Without JS the plain input works.
 */
(function () {
    "use strict";

    function upgrade(input) {
        if (input.dataset.dropzoneReady) return;
        input.dataset.dropzoneReady = "1";
        var zone = document.createElement("div");
        zone.className = "dropzone";
        input.parentNode.insertBefore(zone, input);
        var hint = document.createElement("p");
        hint.className = "dropzone-hint muted small";
        hint.textContent = input.dataset.dropzoneHint || "Drop a file here, or choose one below.";
        var name = document.createElement("p");
        name.className = "dropzone-name";
        name.hidden = true;
        zone.appendChild(hint);
        zone.appendChild(name);
        zone.appendChild(input);

        function showName() {
            var file = input.files && input.files[0];
            name.hidden = !file;
            name.textContent = file ? file.name + " (" + Math.round(file.size / 1048576 * 10) / 10 + " MB)" : "";
        }
        input.addEventListener("change", showName);
        showName();

        ["dragenter", "dragover"].forEach(function (type) {
            zone.addEventListener(type, function (e) {
                e.preventDefault();
                zone.classList.add("is-dragover");
            });
        });
        ["dragleave", "drop"].forEach(function (type) {
            zone.addEventListener(type, function () {
                zone.classList.remove("is-dragover");
            });
        });
        zone.addEventListener("drop", function (e) {
            e.preventDefault();
            var files = e.dataTransfer && e.dataTransfer.files;
            if (!files || !files.length) return;
            var accept = (input.getAttribute("accept") || "").split(",").map(function (s) { return s.trim().toLowerCase(); }).filter(Boolean);
            var file = files[0];
            if (accept.length) {
                var ext = "." + file.name.split(".").pop().toLowerCase();
                var ok = accept.some(function (a) {
                    return a === ext || a === file.type || (a.endsWith("/*") && file.type.indexOf(a.slice(0, -1)) === 0);
                });
                if (!ok) {
                    hint.textContent = "That file type isn't accepted here.";
                    return;
                }
            }
            var dt = new DataTransfer();
            dt.items.add(file);
            input.files = dt.files;
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
    }

    function init() {
        document.querySelectorAll("input[type=file][data-dropzone]").forEach(upgrade);
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
