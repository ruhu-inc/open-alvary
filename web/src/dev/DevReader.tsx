// Development-only preview of the synthetic reader fixture; excluded from production builds.
import fixture from "./reader-fixture.json";
import { ReaderView, type ReaderDescriptor } from "../reader";

export default function DevReader() {
  return (
    <div className="page">
      <div className="page-title">
        <h1>Reader preview</h1>
        <p>
          Development build only. Synthetic test material — not legislation.
        </p>
      </div>
      <ReaderView descriptor={fixture as unknown as ReaderDescriptor} />
    </div>
  );
}
