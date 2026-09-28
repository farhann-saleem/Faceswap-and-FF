export type TextAnimation = "none" | "fade" | "slide-up" | "word-by-word" | "counter" | "keyword-highlight" | "mask-reveal" | "spring-pop" | "tracking-blur" | "map-callout" | "lower-third";
export type Transition = "cut" | "fade" | "push" | "wipe" | "whip" | "flash" | "zoom-blur" | "film-burn" | "map-zoom";

export type CompositionProps = {
  version: 2;
  duration: number;
  width: number;
  height: number;
  fps: number;
  design: {
    font_family: string; font_family_display: string; foreground: string; accent: string;
    danger: string; panel: string; safe_margin: number; grain_opacity: number;
  };
  scenes: Array<{
    id: string; key: string | null; src?: string | null; kind: "video" | "image" | "black";
    timeline_start: number; duration: number; source_start: number;
    crop: { x: number; y: number; w: number; h: number } | null;
    transition: Transition; transition_duration: number;
  }>;
  typography: Array<{
    id: string; beat_id: string | null; start: number; duration: number; text: string; preset: string;
    animation: TextAnimation; x: number; y: number; font_size: number; color: string;
    background_color: string; background_opacity: number; emphasis_words: string[];
    word_timings: Array<{ word: string; start: number; end: number }>;
    timing_source: "aligned" | "provider" | "estimated";
    callout: { label: string; x: number; y: number; detail?: string } | null;
    lower_third: { primary: string; secondary?: string; eyebrow?: string } | null;
  }>;
};
