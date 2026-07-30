/*
 * Enhances every <input type="file"> in the Django admin with drag-and-drop,
 * without removing the native input (so Django's own form handling,
 * validation, and "Currently: ... / Clear" checkbox all keep working as-is).
 *
 * Runs on initial page load AND on Django admin's "formset:added" event, so
 * file inputs added via "Add another" on inline formsets get the same
 * treatment.
 */
(function () {
    function labelFor(input) {
        if (input.files && input.files.length > 0) {
            return input.files[0].name;
        }
        return 'Drag & drop a file here, or click to browse';
    }

    function enhance(input) {
        if (input.dataset.dzEnhanced) return;
        input.dataset.dzEnhanced = 'true';

        var wrapper = document.createElement('div');
        wrapper.className = 'dost-dropzone';

        var label = document.createElement('span');
        label.className = 'dost-dropzone-label';
        label.textContent = labelFor(input);

        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);
        wrapper.appendChild(label);

        function refreshLabel() {
            label.textContent = labelFor(input);
            wrapper.classList.toggle('dost-dropzone--has-file', input.files && input.files.length > 0);
        }
        refreshLabel();

        input.addEventListener('change', function () {
            wrapper.classList.remove('dost-dropzone--dragover');
            refreshLabel();
        });

        ['dragenter', 'dragover'].forEach(function (evt) {
            wrapper.addEventListener(evt, function (e) {
                e.preventDefault();
                e.stopPropagation();
                wrapper.classList.add('dost-dropzone--dragover');
            });
        });

        ['dragleave', 'drop'].forEach(function (evt) {
            wrapper.addEventListener(evt, function (e) {
                e.preventDefault();
                e.stopPropagation();
                wrapper.classList.remove('dost-dropzone--dragover');
            });
        });

        wrapper.addEventListener('drop', function (e) {
            var files = e.dataTransfer.files;
            if (files && files.length > 0) {
                input.files = files;
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }
        });
    }

    function enhanceAll(root) {
        (root || document).querySelectorAll('input[type="file"]').forEach(enhance);
    }

    document.addEventListener('DOMContentLoaded', function () {
        enhanceAll(document);
    });

    // Django admin dispatches this on `document` whenever an inline formset
    // row is added via "Add another" — e.detail.formsetName is available if
    // you ever need to scope this further.
    document.addEventListener('formset:added', function (e) {
        enhanceAll(e.target || document);
    });
})();
