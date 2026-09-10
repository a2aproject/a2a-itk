import ThemeToggle from "./ThemeToggle.tsx";

export default function Header() {
  return (
    <header className="topbar">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <div className="topbar-inner">
        <a className="brand" href="./">
          {/* Official A2A mark; see public/ATTRIBUTION.md. */}
          <img className="brand-mark" src="./a2a-icon.svg" alt="" width="22" height="22" />
          <span>
            A2A <strong>Integration Test Kit</strong>
          </span>
        </a>
        <nav className="topbar-links" aria-label="Resources">
          <a href="https://github.com/a2aproject/a2a-itk#readme">Documentation</a>
          <a href="https://goo.gle/a2a">Specification</a>
          <a href="https://github.com/a2aproject/a2a-itk">GitHub</a>
          <ThemeToggle />
        </nav>
      </div>
    </header>
  );
}
