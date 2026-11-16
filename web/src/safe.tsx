// Guards shared by the public site, the console and the workbench's preview.
import React from "react";

// A part of an address with its %-escapes decoded, or as written when they do not decode
// (/browse#100% must not blank the page).
export function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

// The last resort around a whole app: an error anywhere leaves a message and a way home, not a blank page.
export class RootBoundary extends React.Component<
  { children: React.ReactNode; home: string },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div className="page" role="alert">
        <h1>Something went wrong</h1>
        <p>This page could not be displayed. Reloading it may help.</p>
        <p>
          <a href={this.props.home}>Go to the start</a>
        </p>
      </div>
    ) : (
      this.props.children
    );
  }
}
