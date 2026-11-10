"""Closed session palettes for the installed Streamlit widget contract.

Streamlit's public theme configuration is process-wide; there is no session-local
theme setter. These scoped rules override native foreground/background pairs,
including portal content. Recheck stable test IDs and React Aria structure when
upgrading Streamlit. No browser storage, scripts, or private config mutation.
"""
from types import MappingProxyType


PALETTES = MappingProxyType({
    'Light': MappingProxyType({
        'background':'#F5F7FB', 'sidebar':'#FFFFFF', 'panel':'#FFFFFF',
        'primary':'#0F172A', 'secondary':'#334155', 'metadata':'#475569',
        'border':'#CBD5E1', 'control':'#64748B', 'accent':'#0F766E',
        'on-accent':'#FFFFFF', 'hover':'#F1F5F9',
        'success':'#15803D', 'partial':'#B45309', 'error':'#B91C1C',
        'running':'#1D4ED8', 'pending':'#64748B',
        'red-text':'#991B1B', 'red-bg':'#FEF2F2',
        'orange-text':'#9A3412', 'orange-bg':'#FFF7ED',
        'yellow-text':'#92400E', 'yellow-bg':'#FFFBEB',
        'green-text':'#166534', 'green-bg':'#F0FDF4',
        'blue-text':'#1E40AF', 'blue-bg':'#EFF6FF',
        'violet-text':'#5B21B6', 'violet-bg':'#F5F3FF',
        'gray-text':'#334155', 'gray-bg':'#F1F5F9',
    }),
    'Dark': MappingProxyType({
        'background':'#0B1220', 'sidebar':'#111C2E', 'panel':'#1E293B',
        'primary':'#F8FAFC', 'secondary':'#CBD5E1', 'metadata':'#94A3B8',
        'border':'#475569', 'control':'#94A3B8', 'accent':'#14B8A6',
        'on-accent':'#0B1220', 'hover':'#263244',
        'success':'#22C55E', 'partial':'#F59E0B', 'error':'#F87171',
        'running':'#60A5FA', 'pending':'#94A3B8',
        'red-text':'#FCA5A5', 'red-bg':'#3B1D2A',
        'orange-text':'#FED7AA', 'orange-bg':'#382B15',
        'yellow-text':'#FDE68A', 'yellow-bg':'#382B15',
        'green-text':'#BBF7D0', 'green-bg':'#123429',
        'blue-text':'#BFDBFE', 'blue-bg':'#132D46',
        'violet-text':'#DDD6FE', 'violet-bg':'#2D2442',
        'gray-text':'#E2E8F0', 'gray-bg':'#263244',
    }),
})

_WIDGET_STYLE = """
.stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
[data-testid="stHeader"] {
    color: var(--ns-primary) !important;
    background-color: var(--ns-background) !important;
    background-image: none !important;
}
[data-testid="stSidebar"], [data-testid="stSidebarContent"] {
    color: var(--ns-primary) !important;
    background-color: var(--ns-sidebar) !important;
}
.stApp [class*="st-key-theme-panel-"],
.stApp [data-testid="stExpander"], .stApp [data-testid="stExpander"] details {
    background-color: var(--ns-panel) !important; border-color: var(--ns-border) !important;
}
.stApp [data-testid="stText"], .stApp [data-testid="stText"] span,
.stApp [data-testid="stHeading"], .stApp [data-testid="stHeading"] :is(h1,h2,h3,h4,h5,h6),
.stApp [data-testid="stMarkdownContainer"], .stApp [data-testid="stWidgetLabel"],
.stApp [data-testid="stWidgetLabel"] p,
.stApp [data-testid="stMetricValue"], .stApp [data-testid="stMetricValue"] div,
.stApp [data-testid="stMetricLabel"] {
    color: var(--ns-primary) !important; opacity: 1 !important;
}
/* Native captions fade at the container, not only at the paragraph. */
.stApp [data-testid="stCaptionContainer"], .stApp [data-testid="stCaptionContainer"] p,
.stApp [data-testid="stCaptionContainer"] span {
    color: var(--ns-secondary) !important; opacity: 1 !important;
}
.stApp a { color: var(--ns-accent) !important; text-decoration: underline; }
.stApp hr { border-color: var(--ns-border) !important; }
.stApp :is(input,textarea), .stApp [data-testid="stTextInputRootElement"],
.stApp [data-testid="stNumberInputContainer"], .stApp [data-testid="stTextAreaRootElement"],
.stApp [data-testid="stSelectbox"] [role="combobox"],
.stApp [data-testid="stSelectbox"] [role="group"],
.stApp [data-testid="stSelectbox"] [data-focus-within] {
    color: var(--ns-primary) !important; background-color: var(--ns-panel) !important;
    border-color: var(--ns-control) !important; caret-color: var(--ns-primary);
}
.stApp :is(input,textarea)::placeholder {
    color: var(--ns-metadata) !important; opacity: 1 !important;
}
.stApp :is(input,textarea):disabled {
    color: var(--ns-metadata) !important; -webkit-text-fill-color: var(--ns-metadata) !important;
    opacity: 1 !important; cursor: not-allowed;
}
.stApp button {
    color: var(--ns-primary) !important; background-color: var(--ns-panel) !important;
    border-color: var(--ns-control) !important;
}
.stApp button:hover:not(:disabled) { background-color: var(--ns-hover) !important; }
.stApp [data-testid="stBaseButton-primary"]:not(:disabled) {
    color: var(--ns-on-accent) !important; background-color: var(--ns-accent) !important;
    border-color: var(--ns-accent) !important;
}
.stApp button :is(p,span,[data-testid="stMarkdownContainer"]) { color: inherit !important; }
.stApp button:disabled {
    color: var(--ns-metadata) !important; background-color: var(--ns-panel) !important;
    border-style: dashed; opacity: 1 !important; cursor: not-allowed;
}
.stApp button[role="tab"] { color: var(--ns-secondary) !important; }
.stApp button[role="tab"][aria-selected="true"] {
    color: var(--ns-primary) !important; text-decoration: underline;
    text-decoration-color: var(--ns-accent); text-underline-offset: .35rem;
}
.stApp :is([data-testid="stTabsScrollLeft"],[data-testid="stTabsScrollRight"]) {
    background-image: none !important;
}
.stApp [data-testid="stExpander"] summary {
    color: var(--ns-primary) !important; background-color: var(--ns-panel) !important;
}
/* React Aria state attributes belong to the visible label, not its hidden input. */
.stApp [data-testid="stRadioOption"], .stApp [data-testid="stCheckbox"] label {
    color: var(--ns-primary) !important; background-color: transparent !important;
}
.stApp [data-testid="stRadioOption"] > div > div:first-child,
.stApp [data-testid="stCheckbox"] label > div:first-of-type {
    background-color: var(--ns-panel) !important; border-color: var(--ns-control) !important;
}
.stApp [data-testid="stRadioOption"][data-selected] > div > div:first-child,
.stApp [data-testid="stCheckbox"] label[data-selected] > div:first-of-type {
    background-color: var(--ns-accent) !important; border-color: var(--ns-accent) !important;
}
.stApp [data-testid="stRadioOption"][data-selected] > div > div:first-child > div {
    background-color: var(--ns-on-accent) !important;
}
.stApp [data-testid="stCheckbox"] label[data-selected] > div:first-of-type svg {
    color: var(--ns-on-accent) !important; stroke: var(--ns-on-accent) !important;
}
.stApp :is([data-testid="stRadioOption"],[data-testid="stCheckbox"] label)[data-disabled]
    [data-testid="stMarkdownContainer"] {
    color: var(--ns-metadata) !important; opacity: 1 !important;
}
.stApp :focus-visible, .stApp [data-focus-visible],
.stApp :is([data-testid="stTextInputRootElement"],[data-testid="stTextAreaRootElement"],
    [data-testid="stNumberInputContainer"]):focus-within,
.stApp [data-testid="stSelectbox"] [data-focus-within] {
    outline: 3px solid var(--ns-accent) !important; outline-offset: 3px;
}
/* Popovers/tooltip portals are outside the product shell. */
[data-testid="stSelectboxVirtualDropdown"], [data-testid="stSelectboxVirtualDropdown"] [role="listbox"],
[data-testid="stTooltipContent"], [data-testid="stTooltipContent"] [data-testid="stMarkdownContainer"],
[data-testid="stTooltipErrorContent"], [data-testid="stTooltipErrorContent"] [data-testid="stMarkdownContainer"] {
    color: var(--ns-primary) !important; background-color: var(--ns-panel) !important;
    border-color: var(--ns-control) !important;
}
[data-testid="stSelectboxVirtualDropdown"] [role="option"],
[data-testid="stSelectboxVirtualDropdown"] [role="option"] [data-testid="stMarkdownContainer"] {
    color: var(--ns-primary) !important; background-color: var(--ns-panel) !important;
}
[data-testid="stSelectboxVirtualDropdown"] [role="listbox"][data-empty] [role="option"] > span {
    color: var(--ns-metadata) !important; opacity: 1 !important;
}
[data-testid="stTooltipContent"] a, [data-testid="stTooltipErrorContent"] a {
    color: var(--ns-accent) !important; text-decoration: underline;
}
[data-testid="stSelectboxVirtualDropdown"] [role="option"]:is(:hover,[data-focused],[data-selected]) {
    color: var(--ns-primary) !important; background-color: var(--ns-hover) !important;
}
[data-testid="stSelectboxVirtualDropdown"] [role="option"][data-selected] { font-weight: 700; }
[data-testid="stSelectboxVirtualDropdown"] [role="option"] [data-item-hl] {
    color: inherit !important; background-color: transparent !important;
}
[data-testid="stSelectboxVirtualDropdown"] [role="option"]:is([data-hovered],[data-focused],[data-selected]) [data-item-hl] {
    background-color: var(--ns-hover) !important;
}
[data-testid="stSelectboxVirtualDropdown"] [role="option"][data-focus-visible] {
    outline: 2px solid var(--ns-accent) !important; outline-offset: -2px;
}
/* All existing source/export viewers render plain text, not executed HTML. */
.stApp [data-testid="stCode"], .stApp [data-testid="stCode"] :is(pre,code,span),
.stApp [data-testid="stMarkdownContainer"] code {
    color: var(--ns-primary) !important; background-color: var(--ns-panel) !important;
    text-shadow: none !important;
}
/* Inline native RGB styles cannot track our session palette. Semantic badges get
   app-owned wrappers below; other colored Markdown retains readable neutral pairs. */
.stApp .stMarkdownColoredText { color: var(--ns-primary) !important; }
.stApp :is(.stMarkdownBadge,.stMarkdownColoredBackground) {
    color: var(--ns-gray-text) !important; background-color: var(--ns-gray-bg) !important;
}
"""


def style_for(mode: str) -> str:
    """Emit only static CSS and allowlisted tokens; unknown preferences use Light."""
    mode = mode if mode in PALETTES else 'Light'
    tokens = '\n'.join(f'    --ns-{key}: {value};' for key,value in PALETTES[mode].items())
    rules = [f'<style data-northstar-theme="{mode.lower()}">\n:root {{\n{tokens}\n}}', _WIDGET_STYLE]
    for color in ('red','orange','yellow','green','blue','violet','gray'):
        rules.append(f'''
.stApp [class*="st-key-theme-badge-{color}-"] .stMarkdownBadge {{
    color: var(--ns-{color}-text) !important; background-color: var(--ns-{color}-bg) !important;
}}''')
    for status,color in (('Error','red'),('Warning','yellow'),('Info','blue'),('Success','green')):
        rules.append(f'''
.stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContent{status}"]) {{
    color: var(--ns-{color}-text) !important; background-color: var(--ns-{color}-bg) !important;
}}
.stApp [data-testid="stAlertContent{status}"],
.stApp [data-testid="stAlertContent{status}"] :is(p,span,[data-testid="stMarkdownContainer"]) {{
    color: var(--ns-{color}-text) !important;
}}''')
    rules.append('</style>')
    return '\n'.join(rules)
