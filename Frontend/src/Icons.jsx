// Minimal hand-rolled icon set (stroke-based, lucide-style paths) so the app
// has zero runtime dependency on an icon package.
function Icon({ className = "w-4 h-4", strokeWidth = 2, children }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      {children}
    </svg>
  );
}

const Icons = {
  download: (p) => (
    <Icon {...p}><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></Icon>
  ),
  cpu: (p) => (
    <Icon {...p}><rect x="6" y="6" width="12" height="12" rx="1.5" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" /></Icon>
  ),
  fileText: (p) => (
    <Icon {...p}><path d="M6 2.5h8l4 4V21a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1Z" /><path d="M14 2.5V7h4.5" /><path d="M8 13h8M8 17h8M8 9h3" /></Icon>
  ),
  users: (p) => (
    <Icon {...p}><circle cx="9" cy="8" r="3.2" /><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6" /><circle cx="17" cy="8.5" r="2.6" /><path d="M15.5 14.3c2.9.4 5 2.5 5 5.7" /></Icon>
  ),
  layoutGrid: (p) => (
    <Icon {...p}><rect x="3" y="3" width="7.5" height="7.5" rx="1.2" /><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.2" /><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.2" /><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.2" /></Icon>
  ),
  play: (p) => (
    <Icon {...p}><path d="M6 3.5v17l15-8.5Z" /></Icon>
  ),
  refresh: (p) => (
    <Icon {...p}><path d="M3 12a9 9 0 0 1 15.3-6.4L21 8" /><path d="M21 3v5h-5" /><path d="M21 12a9 9 0 0 1-15.3 6.4L3 16" /><path d="M3 21v-5h5" /></Icon>
  ),
  check: (p) => (
    <Icon {...p}><path d="m5 13 4 4L19 7" /></Icon>
  ),
  checkCircle: (p) => (
    <Icon {...p}><circle cx="12" cy="12" r="9" /><path d="m8.5 12.5 2.3 2.3L16 10" /></Icon>
  ),
  alertTriangle: (p) => (
    <Icon {...p}><path d="M10.6 3.9 2.2 18.5A1.5 1.5 0 0 0 3.5 21h17a1.5 1.5 0 0 0 1.3-2.5L13.4 3.9a1.5 1.5 0 0 0-2.8 0Z" /><path d="M12 9.5v4.2" /><circle cx="12" cy="17" r="0.4" fill="currentColor" /></Icon>
  ),
  x: (p) => (
    <Icon {...p}><path d="M18 6 6 18M6 6l12 12" /></Icon>
  ),
  clock: (p) => (
    <Icon {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5.3l3.5 2" /></Icon>
  ),
  chevronDown: (p) => (
    <Icon {...p}><path d="m6 9 6 6 6-6" /></Icon>
  ),
  edit: (p) => (
    <Icon {...p}><path d="M4 20h4l10.5-10.5a2 2 0 0 0-4-4L4 16v4Z" /><path d="M13 6.5 17.5 11" /></Icon>
  ),
  building: (p) => (
    <Icon {...p}><rect x="4" y="3" width="16" height="18" rx="1" /><path d="M8 7.5h1.5M8 11h1.5M8 14.5h1.5M14.5 7.5H16M14.5 11H16M14.5 14.5H16" /><path d="M9.5 21v-3.5h5V21" /></Icon>
  ),
  loader: (p) => (
    <Icon {...p}><path d="M12 3v3.5" opacity="1" /><path d="M12 17.5V21" opacity="0.3" /><path d="m18.4 5.6-2.5 2.5" opacity="0.85" /><path d="m8.1 15.9-2.5 2.5" opacity="0.45" /><path d="M21 12h-3.5" opacity="0.7" /><path d="M6.5 12H3" opacity="0.6" /><path d="m18.4 18.4-2.5-2.5" opacity="0.55" /><path d="m8.1 8.1-2.5-2.5" opacity="0.4" /></Icon>
  ),
  bell: (p) => (
    <Icon {...p}><path d="M6 9a6 6 0 0 1 12 0c0 4 1.5 5.5 1.5 5.5H4.5S6 13 6 9Z" /><path d="M10 19a2 2 0 0 0 4 0" /></Icon>
  ),
  arrowRight: (p) => (
    <Icon {...p}><path d="M4 12h16M14 6l6 6-6 6" /></Icon>
  ),
  filter: (p) => (
    <Icon {...p}><path d="M4 5h16l-6 7.5V19l-4 2v-8.5Z" /></Icon>
  ),
  database: (p) => (
    <Icon {...p}><ellipse cx="12" cy="5.5" rx="8" ry="3" /><path d="M4 5.5V12c0 1.7 3.6 3 8 3s8-1.3 8-3V5.5" /><path d="M4 12v6.5c0 1.7 3.6 3 8 3s8-1.3 8-3V12" /></Icon>
  ),
  eye: (p) => (
    <Icon {...p}><path d="M2 12s3.5-7.5 10-7.5S22 12 22 12s-3.5 7.5-10 7.5S2 12 2 12Z" /><circle cx="12" cy="12" r="3" /></Icon>
  ),
  trash: (p) => (
    <Icon {...p}><path d="M4 7h16" /><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7" /><path d="M6 7l1 13a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 17 20l1-13" /><path d="M10 11v6M14 11v6" /></Icon>
  ),
  upload: (p) => (
    <Icon {...p}><path d="M12 21V9" /><path d="m7 14 5-5 5 5" /><path d="M5 21h14" /></Icon>
  ),
};

export function AppIcon({ name, className, strokeWidth }) {
  const fn = Icons[name];
  if (!fn) return null;
  return fn({ className, strokeWidth });
}
