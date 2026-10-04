package com.rungic.plasma;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** Husky's Mali must stay outside LXC; bounded app-side retries preserve llvmpipe. */
public final class RenderServerTest {
    static void check(boolean ok, String why) { if (!ok) throw new AssertionError(why); }
    static String state(File dir) throws IOException {
        return new String(Files.readAllBytes(new File(dir,"vtest.state").toPath()), "UTF-8").trim();
    }
    static final class FakeRuntime implements RenderServer.Runtime {
        long now;
        int launches, deaths;
        boolean ready=true, failLaunch;
        long lifetime;
        final File dir;
        final List<Long> delays=new ArrayList<>();
        RenderServer owner;
        int stopAt;
        FakeRuntime(File dir) { this.dir=dir; }
        public long now() { return now; }
        public void pause(long ms) { now+=ms; if(ms>=250)delays.add(ms); }
        public boolean socketReady(File socket) { return ready; }
        public RenderServer.Child launch(File executable, File socket, File log) throws IOException {
            launches++;
            check(executable.getName().equals("libvirgl_test_server.so"), "Execute APK native library, never app data or root");
            check(socket.equals(new File(dir,"vtest.sock")), "Mali vtest must use the shared app tmp directory");
            check(!socket.exists(), "Stale socket must not pretend that a failed server is running");
            check(state(dir).equals("failed"), "llvmpipe must remain available until socket readiness");
            if(failLaunch)throw new IOException("missing executable");
            Files.write(socket.toPath(),new byte[]{1});
            Files.setPosixFilePermissions(socket.toPath(),java.nio.file.attribute.PosixFilePermissions.fromString("rw-------"));
            return new RenderServer.Child() {
                boolean alive=true;
                public boolean alive() { return alive; }
                public int waitFor() throws IOException {
                    check(state(dir).equals("running"), "Only a ready server may advertise virgl");
                    check(Files.getPosixFilePermissions(socket.toPath()).contains(java.nio.file.attribute.PosixFilePermission.OTHERS_WRITE),
                        "Linux UID1000 must connect despite the app's restrictive umask");
                    now+=lifetime; alive=false;
                    if(launches==stopAt)owner.stop();
                    return 1;
                }
                public void destroy() { alive=false; deaths++; }
            };
        }
    }
    // covers: apps.gpu/E6
    static void backoff() {
        RenderServer.Retry retry=new RenderServer.Retry();
        long[] expected={250,500,1000,2000,-1};
        for(long ms:expected)check(retry.afterExit(0)==ms, "Fast Mali failures must back off and give llvmpipe a stable fallback");
        retry=new RenderServer.Retry(); retry.afterExit(0); retry.afterExit(0);
        check(retry.afterExit(10000)==250, "A stable server resets consecutive fast failures");
        check(retry.afterExit(0)==250, "A failure after a stable run starts a new streak");
    }
    // covers: apps.gpu/E6
    static void failed(File dir) throws Exception {
        FakeRuntime runtime=new FakeRuntime(dir);
        RenderServer server=new RenderServer(new File(dir,"native"),dir,false,runtime);
        runtime.owner=server; server.run();
        check(Files.getPosixFilePermissions(new File(dir,"vtest.state").toPath()).contains(java.nio.file.attribute.PosixFilePermission.OTHERS_READ),
            "Linux UID1000 must be able to read fallback state across the shared bind mount");
        check(runtime.launches==5, "Repeated Mali crashes must not consume CPU indefinitely");
        check(runtime.delays.equals(Arrays.asList(250L,500L,1000L,2000L)), "Exponential retry order matters to fallback");
        check(state(dir).equals("failed")&&!new File(dir,"vtest.sock").exists(), "Give-up must remove socket and publish llvmpipe state");
    }
    // covers: apps.gpu/E6
    static void missingAndNotReady(File dir) throws Exception {
        FakeRuntime runtime=new FakeRuntime(dir); runtime.failLaunch=true;
        RenderServer server=new RenderServer(new File(dir,"native"),dir,false,runtime); server.run();
        check(runtime.launches==5&&state(dir).equals("failed"), "Missing native binary must fall back without losing the desktop");
        runtime=new FakeRuntime(dir); runtime.ready=false;
        server=new RenderServer(new File(dir,"native"),dir,false,runtime); server.run();
        check(runtime.launches==5&&runtime.deaths==5&&!new File(dir,"vtest.sock").exists(), "A socket startup timeout must reap each child and permit llvmpipe");
    }
    // covers: apps.gpu/E6
    static void stopAndKgsl(File dir) throws Exception {
        FakeRuntime runtime=new FakeRuntime(dir);
        RenderServer server=new RenderServer(new File(dir,"native"),dir,true,runtime); server.run();
        check(runtime.launches==0, "Qualcomm KGSL must keep its existing renderer and never launch vtest");
        runtime=new FakeRuntime(dir); runtime.stopAt=1; runtime.lifetime=20000;
        server=new RenderServer(new File(dir,"native"),dir,false,runtime); runtime.owner=server; server.run();
        check(runtime.launches==1&&runtime.delays.isEmpty(), "Session stop must prevent restarts, including after a stable run");
        check(state(dir).equals("failed")&&!new File(dir,"vtest.sock").exists(), "Stopped sessions must not leave a stale virgl endpoint");
    }
    // covers: apps.gpu/E6
    static void actualProcessAndStop(File dir) throws Exception {
        File nativeDir=new File(dir,"native"); nativeDir.mkdirs();
        File executable=new File(nativeDir,"libvirgl_test_server.so");
        // Replace EGL with a tiny blocking process: exercise actual ProcessBuilder,
        // readiness and cancellation, without hardware or root on the build host.
        Files.write(executable.toPath(), ("#!/bin/sh\n"+
            "printf '%s\\n' \"$@\"\n"+
            "touch \"$2\"\n"+
            "read reply\n").getBytes("UTF-8"));
        check(executable.setExecutable(true), "Test executable must reside in nativeLibraryDir");
        RenderServer server=new RenderServer(nativeDir,dir,false);
        server.start(); server.start();
        check(state(dir).equals("running"), "Session startup waits for the first listening endpoint");
        String log=new String(Files.readAllBytes(new File(dir,"vtest.log").toPath()),"UTF-8");
        check(log.equals("--socket-path\n"+new File(dir,"vtest.sock").getAbsolutePath()+"\n"),
            "ProcessBuilder must pass a separate absolute socket argument, without shell or su");
        server.stop(); server.stop();
        check(state(dir).equals("failed")&&!new File(dir,"vtest.sock").exists(),
            "Stopping a live process must unblock waitFor and publish fallback before returning");
    }
    public static void main(String[] args) throws Exception {
        File dir=Files.createTempDirectory(new File(args[0]).toPath(),"render-").toFile();
        backoff(); failed(dir); missingAndNotReady(dir); stopAndKgsl(dir); actualProcessAndStop(dir);
        System.out.println("RenderServerTest passed");
    }
}
