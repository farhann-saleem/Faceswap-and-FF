import React from "react";
import { AbsoluteFill, Img, Sequence, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { Video } from "@remotion/media";
import type { CompositionProps, TextAnimation, Transition } from "./types";

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

export function enterStyle(kind: Transition, p: number): React.CSSProperties {
  if (kind === "cut") return {};
  if (kind === "fade") return { opacity: p };
  if (kind === "push") {
    return {
      transform: `translateX(${(1 - p) * 14}%) scale(${1.04 - p * 0.04})`,
      opacity: Math.min(1, p * 2),
    };
  }
  if (kind === "wipe") {
    return {
      clipPath: `inset(0 ${(1 - p) * 100}% 0 0)`,
    };
  }
  if (kind === "whip") {
    return {
      transform: `translateX(${(1 - p) * 34}%) scale(1.06)`,
      filter: `blur(${(1 - p) * 22}px)`,
      opacity: Math.min(1, p * 2.2),
    };
  }
  if (kind === "zoom-blur") {
    return {
      transform: `scale(${1.25 - p * 0.25})`,
      filter: `blur(${(1 - p) * 18}px)`,
      opacity: p,
    };
  }
  if (kind === "map-zoom") {
    return {
      transform: `scale(${1.38 - p * 0.38})`,
      filter: `blur(${(1 - p) * 5}px)`,
      opacity: p,
    };
  }
  if (kind === "flash") {
    const flashDecay = 1 - p;
    return {
      opacity: Math.min(1, p * 3),
      filter: `brightness(${1 + flashDecay * 0.9}) saturate(${1 + flashDecay * 0.2})`,
    };
  }
  if (kind === "film-burn") {
    const burnDecay = 1 - p;
    return {
      opacity: Math.min(1, p * 2.2),
      filter: `brightness(${1 + burnDecay * 0.6}) saturate(${1 + burnDecay * 0.4})`,
    };
  }
  return { opacity: p };
}

const Scene: React.FC<{ scene: CompositionProps["scenes"][number] }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const transitionFrames = Math.max(1, Math.round(scene.transition_duration * fps));
  const p = interpolate(frame, [0, transitionFrames], [0, 1], clamp);
  const crop = scene.crop;
  const mediaStyle: React.CSSProperties = {
    width: crop ? `${100 / crop.w}%` : "100%",
    height: crop ? `${100 / crop.h}%` : "100%",
    objectFit: "cover",
    position: "absolute",
    left: crop ? `${(-crop.x / crop.w) * 100}%` : 0,
    top: crop ? `${(-crop.y / crop.h) * 100}%` : 0,
  };
  const flash = scene.transition === "flash" ? 1 - p : 0;
  const burn = scene.transition === "film-burn" ? 1 - p : 0;

  return (
    <AbsoluteFill style={{ backgroundColor: "#050608", overflow: "hidden" }}>
      <AbsoluteFill style={enterStyle(scene.transition, p)}>
        {scene.kind === "video" && scene.src ? (
          <Video muted src={scene.src} trimBefore={Math.round(scene.source_start * fps)} style={mediaStyle} />
        ) : null}
        {scene.kind === "image" && scene.src ? (
          <Img src={scene.src} style={mediaStyle} />
        ) : null}
      </AbsoluteFill>
      {flash > 0 ? (
        <AbsoluteFill
          style={{
            backgroundColor: "white",
            opacity: flash * 0.92,
            mixBlendMode: "screen",
            pointerEvents: "none",
          }}
        />
      ) : null}
      {burn > 0 ? (
        <AbsoluteFill
          style={{
            background: "radial-gradient(circle at 20% 40%, #fff7ba 0%, #f04b20 28%, #120600 70%)",
            opacity: burn,
            mixBlendMode: "screen",
            pointerEvents: "none",
          }}
        />
      ) : null}
    </AbsoluteFill>
  );
};

function baseMotion(animation: TextAnimation, frame: number, fps: number, durationFrames: number) {
  const enter = interpolate(frame, [0, Math.min(durationFrames / 3, fps * 0.45)], [0, 1], clamp);
  const exit = interpolate(frame, [Math.max(0, durationFrames - fps * 0.28), durationFrames], [1, 0], clamp);
  const visible = Math.min(enter, exit);
  if (animation === "slide-up") return { opacity: visible, transform: `translateY(${(1 - enter) * 34}px)` };
  if (animation === "mask-reveal") return { opacity: exit, clipPath: `inset(0 ${(1 - enter) * 100}% 0 0)` };
  if (animation === "spring-pop") {
    const s = spring({ frame, fps, config: { damping: 13, stiffness: 150, mass: 0.72 } });
    return { opacity: exit, transform: `scale(${0.72 + s * 0.28}) translateY(${(1 - s) * 20}px)` };
  }
  if (animation === "tracking-blur") return { opacity: visible, filter: `blur(${(1 - enter) * 14}px)`, letterSpacing: `${(1 - enter) * 0.24}em` };
  return { opacity: visible };
}

const Counter: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame(); const { fps } = useVideoConfig();
  const match = text.match(/-?[\d,.]+/); if (!match) return <>{text}</>;
  const numeric = Number(match[0].replace(/,/g, "")); if (!Number.isFinite(numeric)) return <>{text}</>;
  const p = spring({ frame, fps, config: { damping: 16, stiffness: 90 } });
  const value = Math.round(numeric * p).toLocaleString("en-US");
  return <>{text.slice(0, match.index)}{value}{text.slice((match.index || 0) + match[0].length)}</>;
};

const KineticWords: React.FC<{ cue: CompositionProps["typography"][number] }> = ({ cue }) => {
  const frame = useCurrentFrame(); const { fps } = useVideoConfig(); const sec = frame / fps;
  const tokens = cue.word_timings.length ? cue.word_timings : cue.text.split(/\s+/).map((word, index, all) => ({ word, start: index * cue.duration / all.length, end: (index + 1) * cue.duration / all.length }));
  return <>{tokens.map((token, index) => {
    const active = sec >= token.start && sec < token.end;
    const revealed = sec >= token.start - 0.04;
    const emphasized = cue.emphasis_words.some((word) => word.toLowerCase().replace(/\W/g, "") === token.word.toLowerCase().replace(/\W/g, ""));
    return <span key={`${token.word}-${index}`} style={{ display: "inline-block", marginRight: "0.24em", opacity: revealed ? 1 : 0.08, color: active || emphasized ? "var(--accent)" : "inherit", transform: `translateY(${revealed ? 0 : 13}px) scale(${active ? 1.06 : 1})`, transition: "none", textShadow: active ? "0 0 28px color-mix(in srgb, var(--accent), transparent 55%)" : undefined }}>{token.word}</span>;
  })}</>;
};

const MapCallout: React.FC<{ cue: CompositionProps["typography"][number] }> = ({ cue }) => {
  const frame = useCurrentFrame(); const { fps } = useVideoConfig(); const s = spring({ frame, fps, config: { damping: 15, stiffness: 120 } });
  const pin = cue.callout || { label: cue.text, x: cue.x, y: cue.y };
  return <AbsoluteFill style={{ pointerEvents: "none" }}>
    <svg width="100%" height="100%" viewBox="0 0 1000 562" preserveAspectRatio="none">
      <circle cx={pin.x * 1000} cy={pin.y * 562} r={7 + s * 13} fill="none" stroke="var(--accent)" strokeWidth="3" opacity={1 - s * 0.65} />
      <circle cx={pin.x * 1000} cy={pin.y * 562} r="7" fill="var(--accent)" />
      <path d={`M ${pin.x * 1000} ${pin.y * 562} L ${pin.x * 1000 + 65 * s} ${pin.y * 562 - 58 * s} L ${pin.x * 1000 + 185 * s} ${pin.y * 562 - 58 * s}`} fill="none" stroke="white" strokeWidth="2" pathLength="1" strokeDasharray="1" strokeDashoffset={1 - s} />
    </svg>
    <div style={{ position: "absolute", left: `${pin.x * 100 + 18.5}%`, top: `${pin.y * 100 - 12}%`, opacity: s, transform: `translateX(${(1 - s) * -12}px)`, textAlign: "left", textTransform: "uppercase", letterSpacing: "0.16em", fontWeight: 800 }}>{pin.label}<div style={{ fontSize: "0.42em", color: "#d6d6d6", letterSpacing: "0.05em", marginTop: 8 }}>{pin.detail}</div></div>
  </AbsoluteFill>;
};

const LowerThird: React.FC<{ cue: CompositionProps["typography"][number] }> = ({ cue }) => {
  const frame = useCurrentFrame(); const { fps } = useVideoConfig(); const p = spring({ frame, fps, config: { damping: 18, stiffness: 150 } });
  const data = cue.lower_third || { primary: cue.text };
  return <div style={{ width: "min(720px, 78vw)", textAlign: "left", transform: `translateX(${(1 - p) * -80}px)`, opacity: p }}>
    <div style={{ width: `${p * 100}%`, height: 4, background: "var(--accent)", marginBottom: 14 }} />
    <div style={{ fontSize: 18, letterSpacing: "0.2em", color: "var(--accent)", fontWeight: 800 }}>{data.eyebrow}</div>
    <div style={{ fontSize: 42, fontWeight: 800 }}>{data.primary}</div>
    {data.secondary ? <div style={{ fontSize: 22, opacity: 0.72, marginTop: 8 }}>{data.secondary}</div> : null}
  </div>;
};

const Typography: React.FC<{ cue: CompositionProps["typography"][number] }> = ({ cue }) => {
  const frame = useCurrentFrame(); const { fps } = useVideoConfig(); const durationFrames = Math.max(1, Math.round(cue.duration * fps));
  if (cue.animation === "map-callout") return <MapCallout cue={cue} />;
  const alignLeft = cue.animation === "lower-third" || cue.preset === "lower-third";
  return <AbsoluteFill style={{ justifyContent: "flex-start", alignItems: alignLeft ? "flex-start" : "center", padding: "5.5%", paddingTop: `${cue.y * 100}%`, color: cue.color, fontFamily: cue.preset === "title" ? "var(--font-display)" : "var(--font)", fontSize: cue.font_size, lineHeight: 1.05, textAlign: alignLeft ? "left" : "center", ...baseMotion(cue.animation, frame, fps, durationFrames) }}>
    {cue.animation === "lower-third" ? <LowerThird cue={cue} /> : <div style={{ maxWidth: "88%", padding: `${cue.background_opacity ? 16 : 0}px ${cue.background_opacity ? 24 : 0}px`, backgroundColor: cue.background_opacity ? `${cue.background_color}${Math.round(cue.background_opacity * 255).toString(16).padStart(2, "0")}` : undefined, boxShadow: cue.background_opacity ? "0 18px 70px rgba(0,0,0,.34)" : undefined, fontWeight: cue.preset === "caption" ? 650 : 850, textTransform: cue.preset === "place" || cue.preset === "map-label" ? "uppercase" : undefined, letterSpacing: cue.preset === "place" ? "0.12em" : undefined }}>
      {cue.animation === "counter" ? <Counter text={cue.text} /> : ["word-by-word", "keyword-highlight"].includes(cue.animation) ? <KineticWords cue={cue} /> : cue.text}
    </div>}
    {cue.timing_source === "estimated" && cue.animation === "word-by-word" ? <div style={{ display: "none" }} data-timing-source="estimated" /> : null}
  </AbsoluteFill>;
};

export const DocumentaryComposition: React.FC<CompositionProps> = (props) => <AbsoluteFill style={{ background: "#050608", color: props.design.foreground, "--accent": props.design.accent, "--font": props.design.font_family, "--font-display": props.design.font_family_display } as React.CSSProperties}>
  {props.scenes.map((scene) => <Sequence key={scene.id} from={Math.round(scene.timeline_start * props.fps)} durationInFrames={Math.max(1, Math.round(scene.duration * props.fps))} premountFor={Math.round(props.fps / 2)}><Scene scene={scene} /></Sequence>)}
  <AbsoluteFill style={{ background: "linear-gradient(180deg, rgba(0,0,0,.12), transparent 38%, rgba(0,0,0,.26))" }} />
  {props.typography.map((cue) => <Sequence key={cue.id} from={Math.round(cue.start * props.fps)} durationInFrames={Math.max(1, Math.round(cue.duration * props.fps))}><Typography cue={cue} /></Sequence>)}
  <AbsoluteFill style={{ opacity: props.design.grain_opacity, mixBlendMode: "screen", backgroundImage: "repeating-radial-gradient(circle at 17% 31%, white 0 0.6px, transparent 0.9px 4px)", backgroundSize: "7px 7px", pointerEvents: "none" }} />
</AbsoluteFill>;
