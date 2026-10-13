# Loaded automatically by gunicorn from the working directory.
logger_class = "platform_core.logredact.GunicornLogger"
accesslog = "-"
# No headers that may carry credentials (cookies, Authorization) and no referer.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss'
