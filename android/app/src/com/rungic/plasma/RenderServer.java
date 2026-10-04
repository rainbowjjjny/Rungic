package com.rungic.plasma;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** App-uid vtest server. No root, GPU device mounts, or Android dependencies in this class. */
public final class RenderServer {
    static final long READY_TIMEOUT_MS=2000, FAST_FAILURE_MS=10000;
    static final int MAX_FAST_FAILURES=5;
    interface Child {
        boolean alive();
        int waitFor() throws IOException, InterruptedException;
        void destroy();
    }
    interface Runtime {
        long now();
        void pause(long ms) throws InterruptedException;
        boolean socketReady(File socket);
        Child launch(File executable, File socket, File log) throws IOException;
    }
    static final class Retry {
        private int fastFailures;
        private long delay=250;
        long afterExit(long lifetimeMs) {
            if(lifetimeMs>=FAST_FAILURE_MS) { fastFailures=0; delay=250; return delay; }
            if(++fastFailures>=MAX_FAST_FAILURES)return -1;
            long result=delay;
            delay=Math.min(delay*2,8000);
            return result;
        }
    }
    private static final Runtime SYSTEM=new Runtime() {
        public long now() { return System.nanoTime()/1000000; }
        public void pause(long ms) throws InterruptedException { Thread.sleep(ms); }
        public boolean socketReady(File socket) { return socket.exists(); }
        public Child launch(File executable, File socket, File log) throws IOException {
            ProcessBuilder builder=new ProcessBuilder(executable.getAbsolutePath(),"--socket-path",socket.getAbsolutePath());
            builder.redirectErrorStream(true).redirectOutput(log); // overwrite per attempt; never block on stdout
            final Process process=builder.start();
            return new Child() {
                public boolean alive() { return process.isAlive(); }
                public int waitFor() throws InterruptedException { return process.waitFor(); }
                public void destroy() {
                    process.destroy();
                    try {
                        if(!process.waitFor(500,TimeUnit.MILLISECONDS))process.destroyForcibly();
                    } catch(InterruptedException e) { process.destroyForcibly(); Thread.currentThread().interrupt(); }
                }
            };
        }
    };
    private final File executable, socket, state, log;
    private final boolean kgsl;
    private final Runtime runtime;
    private final CountDownLatch initialAttempt=new CountDownLatch(1);
    private volatile boolean stopped;
    private Child child;
    private Thread worker;

    public RenderServer(File nativeLibraryDir, File tmpDir, boolean hasKgsl) {
        this(nativeLibraryDir,tmpDir,hasKgsl,SYSTEM);
    }
    RenderServer(File nativeLibraryDir, File tmpDir, boolean hasKgsl, Runtime runtime) {
        executable=new File(nativeLibraryDir,"libvirgl_test_server.so");
        socket=new File(tmpDir,"vtest.sock"); state=new File(tmpDir,"vtest.state");
        log=new File(tmpDir,"vtest.log"); kgsl=hasKgsl; this.runtime=runtime;
    }
    /** Called on the app's startup worker, before starting the Linux session. Bounded wait. */
    public void start() {
        synchronized(this) {
            if(worker!=null||stopped)return;
            worker=new Thread(this::run,"rungic-vtest"); worker.setDaemon(true); worker.start();
        }
        try { initialAttempt.await(READY_TIMEOUT_MS+500,TimeUnit.MILLISECONDS); }
        catch(InterruptedException e) { Thread.currentThread().interrupt(); }
    }
    public void stop() {
        Thread thread;
        synchronized(this) {
            stopped=true;
            if(child!=null)child.destroy();
            thread=worker;
        }
        if(thread!=null&&thread!=Thread.currentThread()) {
            thread.interrupt();
            try { thread.join(1000); } catch(InterruptedException e) { Thread.currentThread().interrupt(); }
        }
        failQuietly();
    }
    private void publish(String value) throws IOException {
        File next=new File(state.getPath()+".new");
        try(FileOutputStream out=new FileOutputStream(next)) {
            out.write((value+"\n").getBytes("UTF-8")); out.getFD().sync();
        }
        if(!next.setReadable(true,false))throw new IOException("Cannot share vtest state");
        if(!next.renameTo(state))throw new IOException("Cannot publish vtest state");
    }
    private synchronized void failed() throws IOException {
        if(socket.exists()&&!socket.delete())throw new IOException("Cannot remove vtest socket");
        publish("failed");
    }
    private void failQuietly() {
        try { failed(); } catch(IOException e) { System.err.println("vtest fallback: "+e); }
    }
    // Package-visible for deterministic tests of the same loop used by the Android app.
    void run() {
        Retry retry=new Retry();
        try {
            File dir=state.getParentFile();
            if(!dir.isDirectory()&&!dir.mkdirs())throw new IOException("Cannot create vtest tmp directory");
            failed();
            if(kgsl||stopped)return;
            while(!stopped) {
                long launched=runtime.now();
                try {
                    synchronized(this) {
                        if(stopped)break;
                        child=runtime.launch(executable,socket,log);
                    }
                    long deadline=runtime.now()+READY_TIMEOUT_MS;
                    while(!stopped&&child.alive()&&!runtime.socketReady(socket)&&runtime.now()<deadline)
                        runtime.pause(50);
                    synchronized(this) {
                        if(stopped)break;
                        if(!child.alive()||!runtime.socketReady(socket))throw new IOException("vtest socket not ready");
                        // Linux UID1000 reaches this socket through the existing private
                        // bind mount; the app's 0077 umask otherwise prevents connect.
                        if(!socket.setReadable(true,false)||!socket.setWritable(true,false))
                            throw new IOException("Cannot share vtest socket");
                        publish("running");
                    }
                    initialAttempt.countDown();
                    child.waitFor();
                } catch(IOException e) {
                    System.err.println("vtest attempt: "+e);
                } finally {
                    synchronized(this) { if(child!=null) { child.destroy(); child=null; } }
                    failed();
                    initialAttempt.countDown();
                }
                if(stopped)break;
                long delay=retry.afterExit(runtime.now()-launched);
                if(delay<0)break;
                runtime.pause(delay);
            }
        } catch(InterruptedException e) {
            Thread.currentThread().interrupt();
        } catch(IOException e) {
            System.err.println("vtest supervisor: "+e);
        } finally { failQuietly(); initialAttempt.countDown(); }
    }
}
