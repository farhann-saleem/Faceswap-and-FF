import React from "react";
import { Composition, registerRoot } from "remotion";
import { DocumentaryComposition } from "./Composition";
import type { CompositionProps } from "./types";

const defaults: CompositionProps = {
  version: 2, duration: 3, width: 1280, height: 720, fps: 30,
  design: { font_family: "Inter, sans-serif", font_family_display: "Georgia, serif", foreground: "#f7f3ea", accent: "#f2b84b", danger: "#ef6045", panel: "#080b10", safe_margin: 0.06, grain_opacity: 0.035 },
  scenes: [{ id: "black", key: null, kind: "black", timeline_start: 0, duration: 3, source_start: 0, crop: null, transition: "fade", transition_duration: 0.3 }],
  typography: [],
};

const Root = () => <Composition id="DocumentaryV2" component={DocumentaryComposition} durationInFrames={90} fps={30} width={1280} height={720} defaultProps={defaults} calculateMetadata={({ props }) => ({ durationInFrames: Math.max(1, Math.round(props.duration * props.fps)), fps: props.fps, width: props.width, height: props.height })} />;
registerRoot(Root);
