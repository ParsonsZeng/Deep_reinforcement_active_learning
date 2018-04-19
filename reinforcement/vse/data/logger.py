# Code referenced from https://gist.github.com/gyglim/1f8dfb1b5c82627ae3efcfbbadb9f514
import requests
import tensorflow as tf
import numpy as np
import scipy.misc
from visdom import Visdom

from config import opt

try:
    from StringIO import StringIO  # Python 2.7
except ImportError:
    from io import BytesIO         # Python 3.x


class LocalLogger(object):
    def __init__(self, log_dir):
        """Create a summary writer logging to log_dir."""
        self.log_dir = log_dir
        self.writer = tf.summary.FileWriter(log_dir)

    def scalar_summary(self, tag, value, step):
        """Log a scalar variable."""
        summary = tf.Summary(
            value=[tf.Summary.Value(tag=tag, simple_value=value)])
        self.writer.add_summary(summary, step)

    def image_summary(self, tag, images, step):
        """Log a list of images."""

        img_summaries = []
        for i, img in enumerate(images):
            # Write the image to a string
            try:
                s = StringIO()
            except:
                s = BytesIO()
            scipy.misc.toimage(img).save(s, format="png")

            # Create an Image object
            img_sum = tf.Summary.Image(encoded_image_string=s.getvalue(),
                                       height=img.shape[0],
                                       width=img.shape[1])
            # Create a Summary value
            img_summaries.append(tf.Summary.Value(
                tag='%s/%d' % (tag, i), image=img_sum))

        # Create and write Summary
        summary = tf.Summary(value=img_summaries)
        self.writer.add_summary(summary, step)

    def histo_summary(self, tag, values, step, bins=1000):
        """Log a histogram of the tensor of values."""

        # Create a histogram using numpy
        counts, bin_edges = np.histogram(values, bins=bins)

        # Fill the fields of the histogram proto
        hist = tf.HistogramProto()
        hist.min = float(np.min(values))
        hist.max = float(np.max(values))
        hist.num = int(np.prod(values.shape))
        hist.sum = float(np.sum(values))
        hist.sum_squares = float(np.sum(values**2))

        # Drop the start of the first bin
        bin_edges = bin_edges[1:]

        # Add bin edges and counts
        for edge in bin_edges:
            hist.bucket_limit.append(edge)
        for c in counts:
            hist.bucket.append(c)

        # Create and write Summary
        summary = tf.Summary(value=[tf.Summary.Value(tag=tag, histo=hist)])
        self.writer.add_summary(summary, step)
        self.writer.flush()

#An external logger so we do not have to write to disk on local server.
class ExternalLogger(object):
    def __init__(self, external_logger_name):
        """Create a summary writer logging to log_dir."""
        self.external_logger_name = external_logger_name

    def scalar_summary(self, tag, value, step):
        """Log a list of images."""

        logdir = self.external_logger_name
        content = {
            'tag': tag,
            'value': value,
            'step': step,
        }
        url = '{}/post_log/{}'.format(opt.external_log_url, logdir)
        res = requests.post(url, json=content)

#A dummy Logger.
class NoLogger(object):

    def scalar_summary(self, tag, value, step):
        pass

class VisdomLogger(object):
    def __init__(self):
        self.vis = Visdom('http://logserver.duckdns.org', port=5010)

    def scalar_summary(self, tag, value, step):
        update = None
        if self.vis.win_exists(tag, opt.logger_name):
            update = 'append'
        self.vis.line(
            Y = np.array([value]),
            X = np.array([step]),
            env = opt.logger_name,
            win = tag,
            update = update,
            opts = dict(
                title = tag
            )
        )
        self.vis.save([opt.logger_name])
