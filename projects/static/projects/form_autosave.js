/* Local autosave for long forms.
 *
 * Safety net for the draft data-loss bug (planning/to-do.md): if a submit
 * dies on the server (crash, timeout, dropped connection), the browser
 * still holds everything the user typed. Text-like fields are snapshotted
 * to localStorage as the user types; when the page next loads with a
 * snapshot that differs from what the server rendered, a banner offers to
 * restore it. If the snapshot matches the rendered values (the save
 * worked), it is discarded silently.
 *
 * Deliberately not covered: file inputs (browsers do not allow refilling
 * them) and rows added dynamically by the contributor formset JS. Opt a
 * form in with the data-autosave attribute.
 */
(function () {
    "use strict";

    var MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000; // snapshots expire after a week
    var DEBOUNCE_MS = 400;

    function fieldsOf(form) {
        var out = [];
        var els = form.querySelectorAll("input, textarea, select");
        for (var i = 0; i < els.length; i++) {
            var el = els[i];
            if (!el.name) continue;
            if (el.type === "file" || el.type === "password" || el.type === "hidden") continue;
            if (el.name.indexOf("csrf") !== -1) continue;
            out.push(el);
        }
        return out;
    }

    function valueOf(el) {
        if (el.type === "checkbox" || el.type === "radio") {
            return el.checked ? "1" : "";
        }
        return el.value;
    }

    function snapshot(form) {
        var data = {};
        var els = fieldsOf(form);
        for (var i = 0; i < els.length; i++) {
            data[els[i].name] = valueOf(els[i]);
        }
        return data;
    }

    function storageAvailable() {
        try {
            var probe = "osprey-autosave-probe";
            window.localStorage.setItem(probe, "1");
            window.localStorage.removeItem(probe);
            return true;
        } catch (err) {
            return false;
        }
    }

    function setup(form) {
        var key = "osprey:formdraft:" + window.location.pathname;
        var timer = null;

        function save() {
            try {
                window.localStorage.setItem(
                    key,
                    JSON.stringify({ ts: Date.now(), fields: snapshot(form) })
                );
            } catch (err) {
                /* storage full or blocked; autosave is best-effort */
            }
        }

        function discard() {
            try {
                window.localStorage.removeItem(key);
            } catch (err) { /* ignore */ }
        }

        function differsFromPage(saved) {
            var els = fieldsOf(form);
            for (var i = 0; i < els.length; i++) {
                var name = els[i].name;
                if (name in saved && saved[name] !== valueOf(els[i])) {
                    return true;
                }
            }
            return false;
        }

        function restore(saved) {
            var els = fieldsOf(form);
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                if (!(el.name in saved)) continue;
                if (el.type === "checkbox" || el.type === "radio") {
                    el.checked = saved[el.name] === "1";
                } else {
                    el.value = saved[el.name];
                }
            }
        }

        function offerRestore(saved, ts) {
            var banner = document.createElement("div");
            banner.className = "callout";
            banner.setAttribute("data-autosave-banner", "");
            var when = new Date(ts);
            var label = document.createElement("span");
            label.textContent =
                "You have unsaved changes to this form from " +
                when.toLocaleString() +
                " (a previous save may not have gone through). ";
            var restoreBtn = document.createElement("button");
            restoreBtn.type = "button";
            restoreBtn.className = "button";
            restoreBtn.textContent = "Restore my text";
            var discardBtn = document.createElement("button");
            discardBtn.type = "button";
            discardBtn.className = "button secondary";
            discardBtn.style.marginLeft = ".5rem";
            discardBtn.textContent = "Discard";
            restoreBtn.addEventListener("click", function () {
                restore(saved);
                banner.remove();
            });
            discardBtn.addEventListener("click", function () {
                discard();
                banner.remove();
            });
            banner.appendChild(label);
            banner.appendChild(restoreBtn);
            banner.appendChild(discardBtn);
            form.parentNode.insertBefore(banner, form);
        }

        // On load: compare any existing snapshot with what the server sent.
        var raw = null;
        try {
            raw = window.localStorage.getItem(key);
        } catch (err) { /* ignore */ }
        if (raw) {
            var parsed = null;
            try {
                parsed = JSON.parse(raw);
            } catch (err) {
                parsed = null;
            }
            if (!parsed || !parsed.fields || Date.now() - parsed.ts > MAX_AGE_MS) {
                discard();
            } else if (differsFromPage(parsed.fields)) {
                offerRestore(parsed.fields, parsed.ts);
            } else {
                // The server rendered exactly what we saved: the earlier
                // submit (or a fresh load of a clean form) succeeded.
                discard();
            }
        }

        form.addEventListener("input", function () {
            if (timer) window.clearTimeout(timer);
            timer = window.setTimeout(save, DEBOUNCE_MS);
        });
        form.addEventListener("change", function () {
            if (timer) window.clearTimeout(timer);
            timer = window.setTimeout(save, DEBOUNCE_MS);
        });
        // One last synchronous snapshot as the form goes out the door, so
        // the freshest text survives even if the request dies.
        form.addEventListener("submit", function () {
            if (timer) window.clearTimeout(timer);
            save();
        });
    }

    if (!storageAvailable()) return;
    document.addEventListener("DOMContentLoaded", function () {
        var forms = document.querySelectorAll("form[data-autosave]");
        for (var i = 0; i < forms.length; i++) {
            setup(forms[i]);
        }
    });
})();
