/*
 * monitoring/static/monitoring/js/site-dropzone.js
 *
 * Adds drag-and-drop to every <input type="file"> rendered on the site's
 * own pages (project photo, requirement uploads, monitoring report
 * uploads, refund receipt / restructure / realignment documents, MSME
 * forms, etc.) — everything reachable from the sidebar tabs, as opposed
 * to dropzone.js which only enhances the Django admin.
 *
 * Same approach as the admin version: the native <input type="file"> is
 * kept in place (wrapped, not replaced) so existing form handling,
 * validation, and any onchange="...submit()" auto-upload behavior (see
 * the project photo control in project_detail.html) keeps working
 * exactly as before. This is purely a visual/UX layer on top.
 */
(function () {
    function labelFor(input) {
        if (input.files && input.files.length > 0) {
            return input.files[0].name;
        }
        return input.dataset.dzPlaceholder || 'Drag & drop a file here, or click to browse';
    }

    function enhance(input) {
        if (input.dataset.siteDzEnhanced) return;
        input.dataset.siteDzEnhanced = 'true';

        var wrapper = document.createElement('div');
        wrapper.className = 'dost-site-dropzone';

        var icon = document.createElement('span');
        icon.className = 'dost-site-dropzone-icon';
        icon.textContent = '⇪';

        var label = document.createElement('span');
        label.className = 'dost-site-dropzone-label';
        label.textContent = labelFor(input);

        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);
        wrapper.appendChild(icon);
        wrapper.appendChild(label);

        function refreshLabel() {
            label.textContent = labelFor(input);
            wrapper.classList.toggle('dost-site-dropzone--has-file', !!(input.files && input.files.length > 0));
        }
        refreshLabel();

        input.addEventListener('change', function () {
            wrapper.classList.remove('dost-site-dropzone--dragover');
            refreshLabel();
        });

        ['dragenter', 'dragover'].forEach(function (evt) {
            wrapper.addEventListener(evt, function (e) {
                e.preventDefault();
                e.stopPropagation();
                wrapper.classList.add('dost-site-dropzone--dragover');
            });
        });

        ['dragleave', 'drop'].forEach(function (evt) {
            wrapper.addEventListener(evt, function (e) {
                e.preventDefault();
                e.stopPropagation();
                wrapper.classList.remove('dost-site-dropzone--dragover');
            });
        });

        wrapper.addEventListener('drop', function (e) {
            var files = e.dataTransfer.files;
            if (files && files.length > 0) {
                input.files = files;
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }
        });

        wrapper.addEventListener('click', function (e) {
            // Clicking anywhere on the zone (not just the tiny native input)
            // opens the file picker, like a normal dropzone.
            if (e.target !== input) input.click();
        });
    }

    function enhanceAll(root) {
        // data-dz-skip opts an input out — used by controls that already
        // have their own custom upload UI (e.g. the project photo's
        // hover-to-change overlay), where wrapping would break the layout.
        (root || document).querySelectorAll('input[type="file"]:not([data-dz-skip])').forEach(enhance);
    }

    document.addEventListener('DOMContentLoaded', function () {
        enhanceAll(document);
    });
})();
