(function () {
    const root = document.documentElement;
    const themeSelect = document.getElementById('theme-select');
    const styleSelect = document.getElementById('style-select');

    function applyTheme(theme) {
        root.dataset.theme = theme;
        localStorage.setItem('woods-keeper-theme', theme);
        if (themeSelect) themeSelect.value = theme;
    }

    function applyStyle(style) {
        root.dataset.style = style;
        localStorage.setItem('woods-keeper-style', style);
        if (styleSelect) styleSelect.value = style;
    }

    applyTheme(localStorage.getItem('woods-keeper-theme') || 'light');
    applyStyle(localStorage.getItem('woods-keeper-style') || 'default');

    if (themeSelect) themeSelect.addEventListener('change', () => applyTheme(themeSelect.value));
    if (styleSelect) styleSelect.addEventListener('change', () => applyStyle(styleSelect.value));
})();
