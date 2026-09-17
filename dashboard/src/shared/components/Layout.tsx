import { Outlet } from "react-router-dom";
import Footer from "./Footer.tsx";
import Header from "./Header.tsx";

/**
 * Chrome shared by every route. The heading and intro are duplicated verbatim
 * in index.html's boot shell so the page says something before React mounts;
 * shell.test.ts holds the two copies together.
 */
export default function Layout() {
  return (
    <>
      <Header />
      <main className="page" id="main">
        <div className="intro">
          <h1>A2A SDK compatibility dashboard</h1>
          <p>
            Nightly results for the <a href="https://goo.gle/a2a">Agent2Agent (A2A)</a>{" "}
            protocol SDKs, over the JSON-RPC, gRPC and HTTP+JSON transports. Two suites
            run every night: interoperability, which exercises each SDK against its
            peers, and conformance, which checks each SDK against the specification.
          </p>
        </div>
        <Outlet />
      </main>
      <Footer />
    </>
  );
}
