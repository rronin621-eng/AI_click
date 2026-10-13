// dock_host.m — AutoConfirm.app 的原生 Cocoa Dock 宿主。
//
// 不能用 Python 宿主：宿主和菜单代理子进程会共享 com.apple.python3 身份，
// macOS 会合并其 GUI 策略，导致 Accessory 状态栏项无法渲染。
// 这个小型原生宿主拥有 AutoConfirm.app 的真实 bundle 身份、负责 Dock；
// Python 子进程以独立的 Python.app 身份作为菜单栏 Accessory 运行。

#import <Cocoa/Cocoa.h>

static NSTask *menuAgent = nil;

static void terminateMenuAgent(void) {
    if (menuAgent != nil && menuAgent.running) {
        [menuAgent terminate];
    }
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        // 主动登记为前台普通应用一次，确保 Dock 为这个无窗口应用创建图标。
        // 不会在之后持续抢焦点，用户随即可切回原应用。
        [app activateIgnoringOtherApps:YES];
        // 显式请求 Dock Tile。部分 macOS 版本对“无窗口的普通应用”只有在
        // 访问 Dock Tile 后才会立刻创建图标。
        [[app dockTile] setBadgeLabel:@""];
        [[app dockTile] display];

        NSFileManager *fm = [NSFileManager defaultManager];
        NSString *logDir = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Logs"];
        NSString *logPath = [logDir stringByAppendingPathComponent:@"auto_confirm_stderr.log"];

        // 1) 安装包版：自带运行环境的菜单栏助手 app，打在本 app 的 Contents/Helpers 里。
        //    它是独立的 bundle（LSUIElement），这样宿主负责 Dock、助手负责顶部状态栏，
        //    两者的 GUI 策略互不干扰；辅助功能权限仍记在 AutoConfirm（宿主）身上。
        NSString *helper = [[[NSBundle mainBundle] bundlePath]
            stringByAppendingPathComponent:
                @"Contents/Helpers/AutoConfirm Menu.app/Contents/MacOS/AutoConfirm Menu"];
        // 2) 开发版：Application Support 里的 venv + 源码（./build_app.sh 部署）。
        NSString *support = [NSHomeDirectory()
            stringByAppendingPathComponent:@"Library/Application Support/AutoConfirm"];
        NSString *python = [support stringByAppendingPathComponent:@"venv/bin/python3"];
        NSString *menuMain = [support stringByAppendingPathComponent:@"menubar_app.py"];

        NSString *executable = nil;
        NSArray<NSString *> *arguments = @[];
        if ([fm isExecutableFileAtPath:helper]) {
            executable = helper;
        } else if ([fm isExecutableFileAtPath:python] && [fm fileExistsAtPath:menuMain]) {
            executable = python;
            arguments = @[menuMain];
        } else {
            NSAlert *alert = [[NSAlert alloc] init];
            alert.messageText = @"AutoConfirm 无法启动";
            alert.informativeText = @"安装不完整：找不到菜单栏组件。请重新下载安装包，"
                                     "把 AutoConfirm 拖进「应用程序」后再打开。";
            [alert runModal];
            return 1;
        }

        [fm createDirectoryAtPath:logDir withIntermediateDirectories:YES attributes:nil error:nil];
        if (![fm fileExistsAtPath:logPath]) {
            [fm createFileAtPath:logPath contents:nil attributes:nil];
        }
        NSFileHandle *logHandle = [NSFileHandle fileHandleForWritingAtPath:logPath];
        [logHandle seekToEndOfFile];

        menuAgent = [[NSTask alloc] init];
        menuAgent.executableURL = [NSURL fileURLWithPath:executable];
        menuAgent.arguments = arguments;

        NSMutableDictionary *environment = [NSMutableDictionary dictionaryWithDictionary:NSProcessInfo.processInfo.environment];
        environment[@"AUTOCONFIRM_MENU_AGENT"] = @"1";
        menuAgent.environment = environment;
        menuAgent.standardOutput = logHandle;
        menuAgent.standardError = logHandle;

        __weak NSApplication *weakApp = app;
        menuAgent.terminationHandler = ^(NSTask *task) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [weakApp terminate:nil];
            });
        };

        NSError *error = nil;
        if (![menuAgent launchAndReturnError:&error]) {
            NSAlert *alert = [[NSAlert alloc] init];
            alert.messageText = @"AutoConfirm 无法启动菜单栏控制器";
            alert.informativeText = error.localizedDescription ?: @"未知启动错误";
            [alert runModal];
            return 1;
        }

        // 普通 App 身份会让 Dock 正确登记此图标；不强制抢占当前前台应用。
        [app activateIgnoringOtherApps:NO];
        [app run];

        terminateMenuAgent();
    }
    return 0;
}
