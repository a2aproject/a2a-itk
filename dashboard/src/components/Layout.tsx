import { Outlet } from "react-router-dom";
import Footer from "./Footer.tsx";
import Header from "./Header.tsx";

/** Chrome shared by every route. */
export default function Layout() {
  return (
    <>
      <Header />
      <main className="page" id="main">
        <div className="intro">
          <h1>A2A SDK compatibility dashboard</h1>
          <p>
            Nightly interoperability results for the{" "}
            <a href="https://goo.gle/a2a">Agent2Agent (A2A) protocol</a> SDKs. Every run
            exercises each SDK against its peers across the JSON-RPC, gRPC and HTTP+JSON
            transports, for both streaming and non-streaming behaviours.
          </p>
        </div>
        <Outlet />
      </main>
      <Footer />
    </>
  );
}
