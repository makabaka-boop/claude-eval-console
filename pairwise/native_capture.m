// Gracefully stoppable capture, including macOS 14. SIGINT on screencapture
// discards its unfinished movie; AVCaptureMovieFileOutput flushes on stop.
#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <signal.h>
#import "native_terminal_capture.m"

@interface Recorder : NSObject <AVCaptureFileOutputRecordingDelegate>
@property NSURL *root;
@property NSURL *output;
@property NSString *target;
@property double seconds;
@property AVCaptureSession *session;
@property AVCaptureMovieFileOutput *movie;
@property NSTimer *timer;
@property NSDate *started;
@property BOOL stopping;
@property NSMutableArray *signals;
@end

@implementation Recorder
- (void)state:(NSString *)phase extra:(NSDictionary *)extra {
    NSMutableDictionary *values = [extra mutableCopy];
    values[@"phase"] = phase;
    values[@"pid"] = @([[NSProcessInfo processInfo] processIdentifier]);
    values[@"updated_at"] = @([[NSDate date] timeIntervalSince1970]);
    NSData *data = [NSJSONSerialization dataWithJSONObject:values options:0 error:nil];
    [data writeToURL:[self.root URLByAppendingPathComponent:@"capture-state.json"] options:NSDataWritingAtomic error:nil];
}
- (void)fail:(NSString *)message {
    [self.session stopRunning];
    [self state:@"error" extra:@{@"error":message}];
    exit(1);
}
- (BOOL)marker:(NSString *)name {
    return [[NSFileManager defaultManager] fileExistsAtPath:[[self.root URLByAppendingPathComponent:name] path]];
}
- (void)stop {
    if (self.stopping) return;
    self.stopping = YES;
    [self state:@"finalizing" extra:@{}];
    [self.movie stopRecording];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 10*NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        [self fail:@"录屏停止后保存超时；已关闭屏幕捕获"];
    });
}
- (void)start {
    [self state:@"starting" extra:@{}];
    if (!CGPreflightScreenCaptureAccess()) {
        [self fail:@"macOS 未授权 Python 屏幕录制；请在系统设置中允许屏幕录制后重试"];
    }
    BOOL cursor = [self.target isEqualToString:@"display"] || [self.target hasPrefix:@"cursor:"];
    NSString *raw = [self.target stringByReplacingOccurrencesOfString:@"cursor:" withString:@""];
    CGDirectDisplayID display = CGMainDisplayID();
    CGRect crop = CGRectZero;
    BOOL cropped = ![raw isEqualToString:@"display"];
    if (cropped) {
        NSArray *parts = [raw componentsSeparatedByString:@","];
        if (parts.count != 4 || [parts[2] doubleValue] <= 0 || [parts[3] doubleValue] <= 0) {
            [self fail:@"无效的录屏区域"];
        }
        CGRect global = CGRectMake([parts[0] doubleValue], [parts[1] doubleValue],
                                   [parts[2] doubleValue], [parts[3] doubleValue]);
        CGDirectDisplayID displays[16];
        uint32_t count = 0;
        CGGetActiveDisplayList(16, displays, &count);
        BOOL found = NO;
        for (uint32_t i = 0; i < count; i++) {
            if (CGRectContainsRect(CGDisplayBounds(displays[i]), global)) {
                display = displays[i]; found = YES; break;
            }
        }
        if (!found) [self fail:@"录屏区域跨越屏幕边界，请将窗口移到一块屏幕内"];
        CGRect bounds = CGDisplayBounds(display);
        // Global top-left -> display-local bottom-left, in screen points.
        crop = CGRectMake(global.origin.x - bounds.origin.x, CGRectGetMaxY(bounds) - CGRectGetMaxY(global),
                          global.size.width, global.size.height);
    }
    AVCaptureScreenInput *input = [[AVCaptureScreenInput alloc] initWithDisplayID:display];
    if (!input) [self fail:@"无法创建屏幕录制输入"];
    input.capturesCursor = cursor;
    input.capturesMouseClicks = cursor;
    input.minFrameDuration = CMTimeMake(1, 30);
    if (cropped) input.cropRect = crop;
    self.session = [[AVCaptureSession alloc] init];
    self.movie = [[AVCaptureMovieFileOutput alloc] init];
    if (![self.session canAddInput:input] || ![self.session canAddOutput:self.movie]) {
        [self fail:@"无法初始化屏幕录制会话"];
    }
    [self.session addInput:input];
    [self.session addOutput:self.movie];
    self.movie.maxRecordedDuration = CMTimeMakeWithSeconds(self.seconds, 600);
    [self.session startRunning];
    [self.movie startRecordingToOutputFileURL:self.output recordingDelegate:self];
    self.signals = [NSMutableArray array];
    for (NSNumber *number in @[@(SIGTERM), @(SIGINT)]) {
        int sig = number.intValue;
        signal(sig, SIG_IGN);
        dispatch_source_t source = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, sig, 0, dispatch_get_main_queue());
        dispatch_source_set_event_handler(source, ^{ [self stop]; });
        dispatch_resume(source);
        [self.signals addObject:source];
    }
    self.timer = [NSTimer scheduledTimerWithTimeInterval:0.1 repeats:YES block:^(NSTimer *timer) {
        if ([self marker:@"done"] || [self marker:@"capture.stop"] ||
            (self.started && -[self.started timeIntervalSinceNow] >= self.seconds)) [self stop];
    }];
}
- (void)captureOutput:(AVCaptureFileOutput *)output didStartRecordingToOutputFileAtURL:(NSURL *)url
      fromConnections:(NSArray<AVCaptureConnection *> *)connections {
    dispatch_async(dispatch_get_main_queue(), ^{
        self.started = [NSDate date];
        if (!self.stopping) [self state:@"recording" extra:@{}];
    });
}
- (void)captureOutput:(AVCaptureFileOutput *)output didFinishRecordingToOutputFileAtURL:(NSURL *)url
      fromConnections:(NSArray<AVCaptureConnection *> *)connections error:(NSError *)error {
    dispatch_async(dispatch_get_main_queue(), ^{
        [self.timer invalidate];
        [self.session stopRunning];
        NSDictionary *info = [[NSFileManager defaultManager] attributesOfItemAtPath:url.path error:nil];
        NSNumber *size = info[NSFileSize] ?: @0;
        BOOL successful = !error || [error.userInfo[AVErrorRecordingSuccessfullyFinishedKey] boolValue];
        BOOL valid = successful && size.unsignedLongLongValue >= 4096;
        double duration = CMTimeGetSeconds(output.recordedDuration);
        [self state:valid ? @"complete" : @"error" extra:@{
            @"size":size, @"duration":@(isfinite(duration) ? duration : 0),
            @"error":valid ? @"" : (error.localizedDescription ?: @"录屏未生成有效视频")
        }];
        exit(valid ? 0 : 1);
    });
}
@end

int run_capture(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 5) return 2;
        if ([@(argv[4]) hasPrefix:@"window:"]) {
            TerminalRecorder *terminal = [[TerminalRecorder alloc] init];
            terminal.root = [NSURL fileURLWithPath:@(argv[1])];
            terminal.output = [NSURL fileURLWithPath:@(argv[2])];
            terminal.seconds = MAX(1, atof(argv[3]));
            terminal.windowID = (CGWindowID)[[@(argv[4]) substringFromIndex:7] longLongValue];
            [terminal start];
            [[NSRunLoop mainRunLoop] run];
            return 0;
        }
        Recorder *recorder = [[Recorder alloc] init];
        recorder.root = [NSURL fileURLWithPath:@(argv[1])];
        recorder.output = [NSURL fileURLWithPath:@(argv[2])];
        recorder.seconds = MAX(1, atof(argv[3]));
        recorder.target = @(argv[4]);
        [recorder start];
        [[NSRunLoop mainRunLoop] run];
    }
    return 0;
}
