package com.rungic.plasma;

import android.app.*;
import android.content.Context;
import android.content.Intent;
import android.os.IBinder;
import java.io.File;

public final class DesktopService extends Service {
    // Owned by the foreground session, so Activity rotation/background does not
    // kill clients. MainActivity calls this on its worker before Wayland/session startup.
    private static RenderServer renderServer;
    static synchronized void startRenderServer(Context context) {
        if(renderServer==null)renderServer=new RenderServer(
            new File(context.getApplicationInfo().nativeLibraryDir),
            new File(context.getFilesDir(),"tmp"),new File("/dev/kgsl-3d0").exists());
        renderServer.start();
    }
    static synchronized void stopRenderServer() {
        if(renderServer!=null) { renderServer.stop(); renderServer=null; }
    }
    @Override public void onDestroy() {
        stopRenderServer();
        super.onDestroy();
    }
    static void update(Context context,String state) {
        context.startForegroundService(new Intent(context,DesktopService.class).putExtra("state",state));
    }
    private Notification notification(String state) {
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this,"desktop").setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle(state).setContentText(getString(R.string.notification_return)).setContentIntent(open)
            .setOnlyAlertOnce(true).setOngoing(true).build();
    }
    @Override public void onCreate() {
        super.onCreate();
        getSystemService(NotificationManager.class).createNotificationChannel(
            new NotificationChannel("desktop","Rungic",NotificationManager.IMPORTANCE_LOW));
        startForeground(1,notification(getString(R.string.state_preparing)));
    }
    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public int onStartCommand(Intent intent,int flags,int id) {
        String state=intent==null?null:intent.getStringExtra("state");
        if(state!=null)getSystemService(NotificationManager.class).notify(1,notification(state));
        return START_NOT_STICKY;
    }
}
