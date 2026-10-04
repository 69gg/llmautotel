export interface AudioMeter {
  resume(): Promise<void>;
  stop(): Promise<void>;
}

/** Reads only transient samples; it never records or exports audio. */
export function createAudioMeter(track: MediaStreamTrack, onLevel: (level: number) => void): AudioMeter | null {
  if (typeof AudioContext === 'undefined') return null;
  const context = new AudioContext();
  const source = context.createMediaStreamSource(new MediaStream([track]));
  const analyser = context.createAnalyser();
  const silent = context.createGain();
  silent.gain.value = 0;
  analyser.fftSize = 256;
  source.connect(analyser);
  analyser.connect(silent);
  silent.connect(context.destination);
  const samples = new Float32Array(analyser.fftSize);
  let active = true;
  let frame = 0;
  const measure = () => {
    if (!active) return;
    analyser.getFloatTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
    onLevel(Math.min(1, rms * 4));
    frame = requestAnimationFrame(measure);
  };
  frame = requestAnimationFrame(measure);
  void context.resume().catch(() => undefined);
  return {
    resume: () => context.resume(),
    stop: async () => {
      if (!active) return;
      active = false;
      cancelAnimationFrame(frame);
      source.disconnect();
      analyser.disconnect();
      silent.disconnect();
      await context.close();
    },
  };
}
