# The service is instantiated by the framework from the manifest; keep its name stable.
-keep class dev.droidctl.agent.AgentService { *; }
# Backend B's entry point is started by name through app_process, never referenced.
-keep class dev.droidctl.agent.ShellMain { public static void main(java.lang.String[]); }
