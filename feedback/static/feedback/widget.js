/* OSPREY feedback widget — floating button, panel, optional annotated
 * screenshot. Logged-in only; the template only includes this file when
 * request.user.is_authenticated.
 */
(function () {
    "use strict";

    var mount = document.getElementById("feedback-widget");
    if (!mount) return;
    var endpoint = mount.dataset.endpoint;
    var csrfToken = mount.dataset.csrf || "";
    if (!endpoint) return;

    // ---- Build DOM ---------------------------------------------------------

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "fb-fab";
    btn.setAttribute("aria-label", "Submit feedback");
    btn.textContent = "Submit feedback";
    mount.appendChild(btn);

    var panel = document.createElement("div");
    panel.className = "fb-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Send feedback");
    panel.hidden = true;
    panel.innerHTML = [
        '<div class="fb-panel-head">',
        '  <strong>Send feedback</strong>',
        '  <button type="button" class="fb-close" aria-label="Close">×</button>',
        '</div>',
        '<div class="fb-panel-body">',
        '  <p class="fb-hint">Idea, bug, something you like about it, or anything else.</p>',
        '  <textarea class="fb-msg" rows="5" maxlength="4000" placeholder="What\'s on your mind?"></textarea>',
        '  <label class="fb-row"><input type="checkbox" class="fb-include-shot" checked> Include a screenshot of this page</label>',
        '  <div class="fb-shot-stage" hidden>',
        '    <button type="button" class="fb-thumb-btn" aria-label="Click to annotate screenshot">',
        '      <img class="fb-thumb" alt="">',
        '      <span class="fb-thumb-overlay">Click to annotate</span>',
        '    </button>',
        '    <p class="fb-shot-status"></p>',
        '  </div>',
        '  <div class="fb-actions">',
        '    <button type="button" class="fb-cancel">Cancel</button>',
        '    <button type="button" class="fb-send">Send</button>',
        '  </div>',
        '  <p class="fb-status" role="status"></p>',
        '</div>',
    ].join("\n");
    mount.appendChild(panel);

    // Editor overlay (full-screen annotation surface).
    var editor = document.createElement("div");
    editor.className = "fb-editor";
    editor.hidden = true;
    editor.innerHTML = [
        '<div class="fb-editor-head">',
        '  <strong>Annotate screenshot</strong>',
        '  <div class="fb-editor-tools">',
        '    <button type="button" class="fb-tool-pen" aria-pressed="true">Draw</button>',
        '    <button type="button" class="fb-tool-undo">Undo</button>',
        '    <button type="button" class="fb-tool-clear">Clear</button>',
        '  </div>',
        '  <button type="button" class="fb-editor-done">Done</button>',
        '</div>',
        '<div class="fb-editor-stage">',
        '  <canvas class="fb-canvas"></canvas>',
        '</div>',
    ].join("\n");
    mount.appendChild(editor);

    var msgEl = panel.querySelector(".fb-msg");
    var includeShot = panel.querySelector(".fb-include-shot");
    var stage = panel.querySelector(".fb-shot-stage");
    var thumbBtn = panel.querySelector(".fb-thumb-btn");
    var thumbImg = panel.querySelector(".fb-thumb");
    var shotStatus = panel.querySelector(".fb-shot-status");
    var statusEl = panel.querySelector(".fb-status");
    var sendBtn = panel.querySelector(".fb-send");
    var cancelBtn = panel.querySelector(".fb-cancel");
    var closeBtn = panel.querySelector(".fb-close");

    var displayCanvas = editor.querySelector(".fb-canvas");
    var doneBtn = editor.querySelector(".fb-editor-done");
    var toolPen = editor.querySelector(".fb-tool-pen");
    var toolUndo = editor.querySelector(".fb-tool-undo");
    var toolClear = editor.querySelector(".fb-tool-clear");

    // ---- Open / close ------------------------------------------------------

    function open() {
        panel.hidden = false;
        btn.hidden = true;
        statusEl.textContent = "";
        if (includeShot.checked) capture();
    }
    function close() {
        panel.hidden = true;
        btn.hidden = false;
        editor.hidden = true;
        resetShot();
        msgEl.value = "";
    }
    btn.addEventListener("click", open);
    closeBtn.addEventListener("click", close);
    cancelBtn.addEventListener("click", close);

    includeShot.addEventListener("change", function () {
        if (includeShot.checked) {
            capture();
        } else {
            resetShot();
        }
    });

    // ---- Screenshot capture ------------------------------------------------

    var baseImage = null;     // HTMLCanvasElement returned by html2canvas
    var strokes = [];         // array of arrays of {x, y}
    var drawing = false;
    var current = null;

    function resetShot() {
        baseImage = null;
        strokes = [];
        thumbImg.removeAttribute("src");
        stage.hidden = true;
    }

    function capture() {
        if (!window.html2canvas) {
            shotStatus.textContent = "Screenshot library not loaded.";
            stage.hidden = false;
            return;
        }
        shotStatus.textContent = "Capturing...";
        stage.hidden = false;
        // Hide our own widget while capturing so it doesn't appear in the shot.
        mount.style.visibility = "hidden";
        window.html2canvas(document.body, {
            backgroundColor: null,
            useCORS: true,
            logging: false,
            scale: 1,
            // Capture only the visible viewport, not the entire scrollable page.
            x: window.scrollX,
            y: window.scrollY,
            width: window.innerWidth,
            height: window.innerHeight,
        }).then(function (cnv) {
            baseImage = cnv;
            displayCanvas.width = cnv.width;
            displayCanvas.height = cnv.height;
            redraw();
            updateThumb();
            shotStatus.textContent = "Click the thumbnail to annotate.";
        }).catch(function (err) {
            shotStatus.textContent = "Couldn't capture screenshot: " + (err && err.message || err);
        }).finally(function () {
            mount.style.visibility = "";
        });
    }

    function redraw() {
        if (!baseImage) return;
        var ctx = displayCanvas.getContext("2d");
        ctx.clearRect(0, 0, displayCanvas.width, displayCanvas.height);
        ctx.drawImage(baseImage, 0, 0);
        ctx.strokeStyle = "#ff2d2d";
        ctx.lineWidth = Math.max(2, Math.round(baseImage.width / 400));
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        strokes.forEach(function (s) {
            if (s.length < 2) return;
            ctx.beginPath();
            ctx.moveTo(s[0].x, s[0].y);
            for (var i = 1; i < s.length; i++) ctx.lineTo(s[i].x, s[i].y);
            ctx.stroke();
        });
    }

    function updateThumb() {
        try {
            thumbImg.src = displayCanvas.toDataURL("image/png");
        } catch (e) {
            // tainted canvas; leave thumbnail empty
        }
    }

    function pointerPos(e) {
        var rect = displayCanvas.getBoundingClientRect();
        var sx = displayCanvas.width / rect.width;
        var sy = displayCanvas.height / rect.height;
        return {
            x: (e.clientX - rect.left) * sx,
            y: (e.clientY - rect.top) * sy,
        };
    }

    displayCanvas.addEventListener("pointerdown", function (e) {
        if (editor.hidden) return;
        displayCanvas.setPointerCapture(e.pointerId);
        drawing = true;
        current = [pointerPos(e)];
        strokes.push(current);
    });
    displayCanvas.addEventListener("pointermove", function (e) {
        if (!drawing) return;
        current.push(pointerPos(e));
        redraw();
    });
    function endStroke() {
        if (!drawing) return;
        drawing = false;
        current = null;
    }
    displayCanvas.addEventListener("pointerup", endStroke);
    displayCanvas.addEventListener("pointercancel", endStroke);
    displayCanvas.addEventListener("pointerleave", endStroke);

    toolUndo.addEventListener("click", function () {
        strokes.pop();
        redraw();
    });
    toolClear.addEventListener("click", function () {
        strokes = [];
        redraw();
    });
    toolPen.addEventListener("click", function () {
        toolPen.setAttribute("aria-pressed", "true");
    });

    // Open editor when the thumbnail is clicked.
    thumbBtn.addEventListener("click", function () {
        if (!baseImage) return;
        editor.hidden = false;
        document.body.classList.add("fb-editor-open");
    });

    function closeEditor() {
        editor.hidden = true;
        document.body.classList.remove("fb-editor-open");
        updateThumb();
    }
    doneBtn.addEventListener("click", closeEditor);
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !editor.hidden) closeEditor();
    });

    // ---- Submit ------------------------------------------------------------

    function send() {
        var msg = msgEl.value.trim();
        if (!msg) {
            statusEl.textContent = "Please write a message.";
            return;
        }
        sendBtn.disabled = true;
        statusEl.textContent = "Sending...";

        var fd = new FormData();
        fd.append("message", msg);
        fd.append("page_url", window.location.href);
        fd.append("page_title", document.title || "");
        fd.append("viewport_w", String(window.innerWidth));
        fd.append("viewport_h", String(window.innerHeight));
        if (includeShot.checked && baseImage) {
            try {
                fd.append("screenshot", displayCanvas.toDataURL("image/png"));
            } catch (e) {
                // Tainted canvas (cross-origin image). Submit without shot.
            }
        }

        fetch(endpoint, {
            method: "POST",
            credentials: "same-origin",
            headers: { "X-CSRFToken": csrfToken },
            body: fd,
        }).then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
        }).then(function () {
            statusEl.textContent = "Thanks. Got it.";
            setTimeout(close, 1200);
        }).catch(function (err) {
            statusEl.textContent = "Send failed: " + err.message;
        }).finally(function () {
            sendBtn.disabled = false;
        });
    }

    sendBtn.addEventListener("click", send);
})();
