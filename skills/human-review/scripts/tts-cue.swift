// Speak one narration cue to a .wav and report where every word lands in it.
//
// `say(1)` can produce the audio but not the timings, and without per-word timings the
// karaoke captions can only be guessed at from string length — which drifts within a
// sentence and reads as sloppy exactly where the eye is looking. AVSpeechSynthesizer's
// `willSpeakRangeOfSpeechString:` fires while `write(_:toBufferCallback:)` renders, so
// counting frames written at the moment each range arrives gives the true offset of every
// word in the file it just produced.
//
// Ranges are UTF-16 offsets into the utterance, so the substring is emitted alongside them
// — the caller matches on text and never has to reproduce UTF-16 arithmetic.
//
// Usage: tts-cue <text> <out.wav> [voice] [rate]   -> timings JSON on stdout

import AVFoundation
import Foundation

final class Marks: NSObject, AVSpeechSynthesizerDelegate {
  var marks: [[String: Any]] = []
  var frames: AVAudioFramePosition = 0
  var sampleRate: Double = 22050
  var text: NSString = ""

  func speechSynthesizer(_ synth: AVSpeechSynthesizer,
                         willSpeakRangeOfSpeechString range: NSRange,
                         utterance: AVSpeechUtterance) {
    marks.append([
      "location": range.location,
      "word": text.substring(with: range),
      "t": Double(frames) / sampleRate,
    ])
  }
}

let argv = CommandLine.arguments
guard argv.count >= 3 else {
  FileHandle.standardError.write("usage: tts-cue <text> <out.wav> [voice] [rate]\n".data(using: .utf8)!)
  exit(2)
}
let text = argv[1]
let outURL = URL(fileURLWithPath: argv[2])
let voiceName = argv.count > 3 && !argv[3].isEmpty ? argv[3] : "Samantha"
let rate = argv.count > 4 && !argv[4].isEmpty ? Float(argv[4]) ?? AVSpeechUtteranceDefaultSpeechRate
                                              : AVSpeechUtteranceDefaultSpeechRate

let utterance = AVSpeechUtterance(string: text)
utterance.rate = rate
utterance.preUtteranceDelay = 0
utterance.postUtteranceDelay = 0
if let voice = AVSpeechSynthesisVoice.speechVoices().first(where: { $0.name == voiceName }) {
  utterance.voice = voice
} else {
  FileHandle.standardError.write("[tts] no voice named \"\(voiceName)\"; using the default\n"
      .data(using: .utf8)!)
}

let collector = Marks()
collector.text = text as NSString
let synth = AVSpeechSynthesizer()
synth.delegate = collector

var file: AVAudioFile?
var failure: String?
var finished = false

synth.write(utterance) { buffer in
  guard let pcm = buffer as? AVAudioPCMBuffer else { return }
  // A zero-length buffer is how `write` signals the end of the utterance.
  if pcm.frameLength == 0 { finished = true; return }
  if file == nil {
    collector.sampleRate = pcm.format.sampleRate
    let settings: [String: Any] = [
      AVFormatIDKey: kAudioFormatLinearPCM,
      AVSampleRateKey: pcm.format.sampleRate,
      AVNumberOfChannelsKey: pcm.format.channelCount,
      AVLinearPCMBitDepthKey: 16,
      AVLinearPCMIsFloatKey: false,
      AVLinearPCMIsBigEndianKey: false,
    ]
    do {
      // The processing format is pinned to the buffer's own so `write(from:)` never has to
      // convert; only the on-disk representation is 16-bit.
      file = try AVAudioFile(forWriting: outURL, settings: settings,
                             commonFormat: pcm.format.commonFormat,
                             interleaved: pcm.format.isInterleaved)
    } catch { failure = "\(error)"; finished = true; return }
  }
  do { try file?.write(from: pcm) } catch { failure = "\(error)"; finished = true; return }
  collector.frames += AVAudioFramePosition(pcm.frameLength)
}

// `write` calls back on the main queue, so the main thread has to keep running its run loop
// rather than block on a semaphore — blocking here yields a silent, empty result.
let deadline = Date().addingTimeInterval(120)
while !finished && Date() < deadline {
  RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
}

if let failure {
  FileHandle.standardError.write("[tts] \(failure)\n".data(using: .utf8)!)
  exit(1)
}
if collector.frames == 0 {
  FileHandle.standardError.write("[tts] the synthesizer produced no audio\n".data(using: .utf8)!)
  exit(1)
}

let payload: [String: Any] = [
  "duration": Double(collector.frames) / collector.sampleRate,
  "sampleRate": collector.sampleRate,
  "voice": utterance.voice?.name ?? voiceName,
  "marks": collector.marks,
]
FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: payload))
