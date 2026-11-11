// Terminal-only capture. Included by native_capture.m so Python.app keeps its
// existing screen-recording permission. No Accessibility permission is needed.
#import <AppKit/AppKit.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>

@interface TerminalRecorder : NSObject <SCStreamOutput, SCStreamDelegate> {
    CMSampleBufferRef _lastSample;
}
@property NSURL *root;
@property NSURL *output;
@property CGWindowID windowID;
@property double seconds;
@property SCStream *stream;
@property AVAssetWriter *writer;
@property AVAssetWriterInput *video;
@property NSTimer *timer;
@property NSDate *started;
@property CMTime firstTime;
@property CGSize windowSize;
@property CGRect contentRect;
@property BOOL stopping;
@property NSString *failure;
@property NSMutableArray *signals;
@end

@implementation TerminalRecorder
- (void)state:(NSString *)phase extra:(NSDictionary *)extra {
    NSMutableDictionary *values = [extra mutableCopy];
    values[@"phase"] = phase;
    values[@"pid"] = @([[NSProcessInfo processInfo] processIdentifier]);
    values[@"updated_at"] = @([[NSDate date] timeIntervalSince1970]);
    values[@"target"] = [NSString stringWithFormat:@"window:%u", self.windowID];
    values[@"cursor"] = @NO;
    values[@"content_rect"] = @[@(self.contentRect.origin.x), @(self.contentRect.origin.y),
                                  @(self.contentRect.size.width), @(self.contentRect.size.height)];
    NSData *data = [NSJSONSerialization dataWithJSONObject:values options:0 error:nil];
    [data writeToURL:[self.root URLByAppendingPathComponent:@"capture-state.json"] options:NSDataWritingAtomic error:nil];
}
- (BOOL)marker:(NSString *)name {
    return [[NSFileManager defaultManager] fileExistsAtPath:[[self.root URLByAppendingPathComponent:name] path]];
}
- (void)fail:(NSString *)message {
    self.failure = message;
    if (self.started && !self.stopping) { [self stop]; return; }
    [self state:@"error" extra:@{@"error": message}];
    exit(1);
}
- (void)stop {
    if (self.stopping) return;
    self.stopping = YES;
    [self.timer invalidate];
    [self state:@"finalizing" extra:@{}];
    [self.stream stopCaptureWithCompletionHandler:^(NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (!self.started) { [self fail:@"终端录屏未收到有效画面"]; return; }
            // ScreenCaptureKit can omit unchanged frames. Extend the last real
            // frame to the stop time so pauses for reading survive encoding.
            double elapsed = MIN(self.seconds, -[self.started timeIntervalSinceNow]);
            CMTime end = CMTimeAdd(self.firstTime, CMTimeMakeWithSeconds(elapsed, 600));
            if (self->_lastSample && self.video.readyForMoreMediaData &&
                CMTimeCompare(end, CMSampleBufferGetPresentationTimeStamp(self->_lastSample)) > 0) {
                CMSampleTimingInfo timing = {CMTimeMake(1, 30), end, kCMTimeInvalid};
                CMSampleBufferRef tail = NULL;
                if (CMSampleBufferCreateCopyWithNewTiming(kCFAllocatorDefault, self->_lastSample, 1, &timing, &tail) == noErr) {
                    if (![self.video appendSampleBuffer:tail]) self.failure = self.writer.error.localizedDescription;
                    CFRelease(tail);
                }
            }
            [self.writer endSessionAtSourceTime:end];
            [self.video markAsFinished];
            [self.writer finishWritingWithCompletionHandler:^{
                dispatch_async(dispatch_get_main_queue(), ^{
                    NSDictionary *info = [[NSFileManager defaultManager] attributesOfItemAtPath:self.output.path error:nil];
                    NSNumber *size = info[NSFileSize] ?: @0;
                    BOOL valid = self.writer.status == AVAssetWriterStatusCompleted && size.unsignedLongLongValue >= 4096 && !self.failure;
                    [self state:valid ? @"complete" : @"error" extra:@{
                        @"size": size, @"duration": @(elapsed),
                        @"error": valid ? @"" : (self.failure ?: self.writer.error.localizedDescription ?: @"终端视频保存失败")
                    }];
                    exit(valid ? 0 : 1);
                });
            }];
        });
    }];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 10*NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        [self fail:@"终端录屏停止后保存超时"];
    });
}
- (void)configure:(SCWindow *)window {
    if (![window.owningApplication.bundleIdentifier isEqualToString:@"com.apple.Terminal"] ||
        !window.onScreen || window.frame.size.width < 320 || window.frame.size.height < 240) {
        [self fail:@"录屏目标不是可见的 Terminal 窗口"]; return;
    }
    self.windowSize = window.frame.size;
    SCContentFilter *filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:window];
    // Use AppKit's current title-bar metrics rather than desktop/menu offsets.
    // The Python driver reuses an existing standard, single-tab Terminal window.
    NSRect frame = NSMakeRect(0, 0, self.windowSize.width, self.windowSize.height);
    NSRect content = [NSWindow contentRectForFrameRect:frame styleMask:NSWindowStyleMaskTitled];
    CGFloat titleHeight = frame.size.height - content.size.height;
    self.contentRect = CGRectMake(1, titleHeight, floor(content.size.width - 2), floor(content.size.height - 1));
    SCStreamConfiguration *config = [[SCStreamConfiguration alloc] init];
    config.sourceRect = self.contentRect;
    config.width = (NSUInteger)self.contentRect.size.width / 2 * 2;
    config.height = (NSUInteger)self.contentRect.size.height / 2 * 2;
    config.minimumFrameInterval = CMTimeMake(1, 30);
    config.queueDepth = 4;
    config.pixelFormat = kCVPixelFormatType_32BGRA;
    config.showsCursor = NO;
    config.capturesAudio = NO;
    config.ignoreShadowsSingleWindow = YES;
    config.ignoreGlobalClipSingleWindow = YES;
    config.shouldBeOpaque = YES;
    // Park inside the window without clicking. Never trigger a menu or Dock.
    CGWarpMouseCursorPosition(CGPointMake(CGRectGetMidX(window.frame), CGRectGetMidY(window.frame)));
    NSError *error = nil;
    self.writer = [[AVAssetWriter alloc] initWithURL:self.output fileType:AVFileTypeQuickTimeMovie error:&error];
    self.video = [[AVAssetWriterInput alloc] initWithMediaType:AVMediaTypeVideo outputSettings:@{
        AVVideoCodecKey: AVVideoCodecTypeH264, AVVideoWidthKey: @(config.width), AVVideoHeightKey: @(config.height)
    }];
    self.video.expectsMediaDataInRealTime = YES;
    if (!self.writer || ![self.writer canAddInput:self.video]) {
        [self fail:error.localizedDescription ?: @"无法创建终端视频编码器"]; return;
    }
    [self.writer addInput:self.video];
    if (![self.writer startWriting]) { [self fail:self.writer.error.localizedDescription]; return; }
    self.stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:self];
    if (![self.stream addStreamOutput:self type:SCStreamOutputTypeScreen sampleHandlerQueue:dispatch_get_main_queue() error:&error]) {
        [self fail:error.localizedDescription]; return;
    }
    [self.stream startCaptureWithCompletionHandler:^(NSError *startError) {
        if (startError) dispatch_async(dispatch_get_main_queue(), ^{ [self fail:startError.localizedDescription]; });
    }];
    self.timer = [NSTimer scheduledTimerWithTimeInterval:0.2 repeats:YES block:^(NSTimer *timer) {
        if ([self marker:@"done"] || [self marker:@"capture.stop"] ||
            (self.started && -[self.started timeIntervalSinceNow] >= self.seconds)) { [self stop]; return; }
        NSArray *windows = CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, self.windowID));
        CGRect current = CGRectZero;
        if (!windows.count || !CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)windows[0][(__bridge NSString *)kCGWindowBounds], &current)) {
            [self fail:@"录屏终端窗口已关闭"]; return;
        }
        if (![windows[0][(__bridge NSString *)kCGWindowIsOnscreen] boolValue]) {
            [self fail:@"录屏终端窗口已最小化或隐藏，请重新录制"]; return;
        }
        if (!CGSizeEqualToSize(current.size, self.windowSize)) [self fail:@"录屏终端窗口尺寸发生变化，请重新录制"];
    }];
}
- (void)start {
    // Python.app loads this library without an AppKit application of its own.
    // ScreenCaptureKit/NSWindow metrics require a WindowServer connection.
    [NSApplication sharedApplication];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
    [self state:@"starting" extra:@{}];
    if (!CGPreflightScreenCaptureAccess()) { [self fail:@"macOS 未授权 Python 屏幕录制"]; return; }
    self.signals = [NSMutableArray array];
    for (NSNumber *number in @[@(SIGTERM), @(SIGINT)]) {
        int sig = number.intValue;
        signal(sig, SIG_IGN);
        dispatch_source_t source = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, sig, 0, dispatch_get_main_queue());
        dispatch_source_set_event_handler(source, ^{ [self stop]; });
        dispatch_resume(source);
        [self.signals addObject:source];
    }
    [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES completionHandler:^(SCShareableContent *content, NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (error) { [self fail:error.localizedDescription]; return; }
            for (SCWindow *window in content.windows) {
                if (window.windowID == self.windowID) { [self configure:window]; return; }
            }
            [self fail:@"无法找到指定终端窗口，已停止录屏；不会回退到整屏录制"];
        });
    }];
}
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(SCStreamOutputType)type {
    if (self.stopping || type != SCStreamOutputTypeScreen || !CMSampleBufferIsValid(sample)) return;
    NSArray *attachments = (__bridge NSArray *)CMSampleBufferGetSampleAttachmentsArray(sample, false);
    NSNumber *status = attachments.firstObject[SCStreamFrameInfoStatus];
    if (!status || status.integerValue != SCFrameStatusComplete || !self.video.readyForMoreMediaData) return;
    if (!self.started) {
        self.firstTime = CMSampleBufferGetPresentationTimeStamp(sample);
        [self.writer startSessionAtSourceTime:self.firstTime];
        self.started = [NSDate date];
    }
    if (![self.video appendSampleBuffer:sample]) { [self fail:self.writer.error.localizedDescription ?: @"终端视频写入失败"]; return; }
    BOOL first = !_lastSample;
    if (_lastSample) CFRelease(_lastSample);
    _lastSample = (CMSampleBufferRef)CFRetain(sample);
    if (first) [self state:@"recording" extra:@{}];
}
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    dispatch_async(dispatch_get_main_queue(), ^{ if (!self.stopping) [self fail:error.localizedDescription]; });
}
- (void)dealloc { if (_lastSample) CFRelease(_lastSample); }
@end
