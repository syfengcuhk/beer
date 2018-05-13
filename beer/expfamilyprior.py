
# pylint: disable=E1102
# pylint: disable=C0103

import abc
import math
import torch
import torch.autograd as ta


def _bregman_divergence(f_val1, f_val2, grad_f_val2, val1, val2):
    return f_val1 - f_val2 - grad_f_val2 @ (val1 - val2)


# The following code compute the log of the determinant of a
# positive definite matrix. This is equivalent to:
#   >>> torch.log(torch.det(mat))
# Note: the hook is necessary to correct the gradient as pytorch
# will return upper triangular gradient.
def _logdet(mat):
    if mat.requires_grad:
        mat.register_hook(lambda grad: .5 * (grad + grad.t()))
    return 2 * torch.log(torch.diag(torch.potrf(mat))).sum()


class ExpFamilyPrior(metaclass=abc.ABCMeta):
    '''Abstract base class for (conjugate) priors from the exponential
    family of distribution. Prior distributions subclassing
    ``ExpFamilyPrior`` are of the form:

    .. math::
       p(x | \\theta ) = \\exp \\big\\{ \\eta(\\theta)^T T(x)
        - A\\big(\\eta(\\theta) \\big) \\big\\}

    where

      * :math:`x` is the parameter for a model for which we want to
        have a prior/posterior distribution.
      * :math:`\\theta` is the set of *standard hyper-parameters*
      * :math:`\\eta(\\theta)` is the vector of *natural
        hyper-parameters*
      * :math:`T(x)` are the sufficient statistics.
      * :math:`A\\big(\\eta(\\theta) \\big)` is the log-normalizing
        function

    '''

    # pylint: disable=W0102
    def __init__(self, natural_hparams):
        '''Initialize the base class.

        Args:
            natural_hparams (``torch.Tensor``): Natural hyper-parameters
                of the distribution.

        Note:
            When subclassing ``beer.ExpFamilyPrior``, the child class
            should call the ``__init__`` method.

            .. code-block:: python

               class MyPrior(beer.ExpFamilyPrior):

                   def __init__(self, std_hparams):
                        # Transfrom the standard hyper-parameters into
                        # the natural hyper-parameters.
                        natural_hparams = transform(std_hparams)
                        super().__init__(natural_hparams)

                   ...

        '''
        # This will be initialized when setting the natural params
        # property.
        self._expected_sufficient_statistics = None
        self._natural_hparams = None

        self.natural_hparams = natural_hparams

    @property
    def expected_sufficient_statistics(self):
        '''``torch.Tensor``: Expected value of the sufficient statistics.

        .. math::
           \\langle T(x) \\rangle_{p(x | \\theta)} = \\nabla_{\\eta} \\;
                A\\big(\\eta(\\theta) \\big)

        '''
        return self._expected_sufficient_statistics.data

    @property
    def natural_hparams(self):
        '``torch.Tensor``: Natural hyper-parameters vector.'
        return self._natural_hparams.data

    @natural_hparams.setter
    def natural_hparams(self, value):
        if value.grad is not None:
            value.grad.zero_()()
        log_norm_value = self.log_norm(value)
        ta.backward(log_norm_value)
        self._expected_sufficient_statistics = value.grad
        self._natural_hparams = value

    @abc.abstractmethod
    def split_sufficient_statistics(self, s_stats):
        '''Abstract method to be implemented by subclasses of
        ``beer.ExpFamilyPrior``.

        Split the sufficient statistics vector into meaningful groups.
        The notion of *meaningful group* depends on the type of the
        subclass. For instance, the sufficient statistics of the
        Normal density are :math:`T(x) = (x^2, x)^T` leading to
        the following groups: :math:`x^2` and :math:`x`.

        Args:
            s_stats (``torch.Tensor``): Sufficients statistics to
                split

        Returns:
            A ``torch.Tensor`` or a tuple of ``torch.Tensor`` depending
            on the type of density.

        '''
        pass

    @abc.abstractmethod
    def log_norm(self, natural_hparams):
        '''Abstract method to be implemented by subclasses of
        ``beer.ExpFamilyPrior``.

        Log-normalizing function of the density.

        Args:
            natural_hparams (``torch.Tensor``): Natural hyper-parameters
                of the distribution.

        Returns:
            ``torch.Tensor`` of size 1: Log-normalization value.

        '''
        pass


class DirichletPrior(ExpFamilyPrior):
    '''The Dirichlet density defined as:

    .. math::
       p(x | \\alpha) = \\frac{\\Gamma(\\sum_{i=1}^K \\alpha_i)}
            {\\prod_{i=1}^K \\Gamma(\\alpha_i)}
            \\prod_{i=1}^K x_i^{\\alpha_i - 1}

    where :math:`\\alpha` is the concentration parameter.

    '''

    def __init__(self, concentrations):
        '''
        Args:
            concentrations (``torch.Tensor``): Concentration for each
                dimension.
        '''
        natural_hparams = torch.tensor(concentrations - 1, requires_grad=True)
        super().__init__(natural_hparams)

    def split_sufficient_statistics(self, s_stats):
        '''For the Dirichichlet density, this is simply the identity
        function as there is only a single "group" of sufficient
        statistics.

        Args:
            s_stats (``torch.Tensor``): Sufficients statistics to
                split

        Returns:
            ``torch.Tensor``: ``s_stats`` unchanged.

        '''
        return s_stats

    def log_norm(self, natural_hparams):
        '''Log-normalizing function

        Args:
            natural_hparams (``torch.Tensor``): Natural hyper-parameters
                of the distribution.

        Returns:
            ``torch.Tensor`` of size 1: Log-normalization value.

        '''
        return - torch.lgamma((natural_hparams + 1).sum()) +\
            torch.lgamma(natural_hparams + 1).sum()


class NormalGammaPrior(ExpFamilyPrior):
    '''The Normal-Gamma density defined as:

    .. math::
       p(\\mu, \\lambda | m, \\kappa, a, b) = \\mathcal{N} \\big(\mu | m,
        (\\kappa \; \\text{diag}(\\lambda))^{-1} \\big)
        \\mathcal{G} \\big( \\lambda | a, b \\big)

    where:

      * :math:`\\mu`, :math:`\\lambda` are the mean and the diagonal
        of the precision matrix of a multivariate normal density.
      * :math:`m` is the hyper-parameter mean of the Normal density.
      * :math:`\\kappa` is the hyper-parameter scale of the Normal
        density.
      * :math:`a` is the hyper-paramater shape of the Gamma density.
      * :math:`b` is the hyper-parameter rate of the Gamma density.

    Note:
        Strictly speaking, the Normal-Gamma density is a
        distribution over a 1 dimensional mean and precision parameter.
        In our case, :math:`\\mu` and :math:`\\lambda` are a
        D-dimensional vector and the diagonal of a :math:`D \\times D`
        precision matrix respectively. The ``beer.NormalGammaPrior``
        can be seen as the concatenation of :math:`D` indenpendent
        "standard" Normal-Gamma densities.

    '''

    def __init__(self, mean, scale, shape, rate):
        '''
        Args:
            mean (``torch.Tensor``): Mean of the Normal.
            scale (``torch.Tensor``): Scale of the Normal.
            shape (``torch.Tensor``): Shape parameter of the Gamma.
            rate (``torch.Tensor``): Rate parameter of the Gamma.

        '''
        natural_hparams = torch.tensor(torch.cat([
            scale * (mean ** 2) + 2 * rate,
            scale * mean,
            scale,
            2 * shape - 1
        ]), requires_grad=True)
        super().__init__(natural_hparams)

    def split_sufficient_statistics(self, s_stats):
        '''Split the sufficient statistics into 4 groups.

        Args:
            s_stats (``torch.Tensor``): Sufficients statistics to
                split

        Returns:
            ``torch.Tensor``: ``s_stats`` unchanged.

        '''
        return tuple(s_stats.view(4, -1))

    def log_norm(self, natural_hparams):
        '''Log-normalizing function

        Args:
            natural_hparams (``torch.Tensor``): Natural hyper-parameters
                of the distribution.

        Returns:
            ``torch.Tensor`` of size 1: Log-normalization value.

        '''
        np1, np2, np3, np4 = self.split_sufficient_statistics(natural_hparams)
        lognorm = torch.lgamma(.5 * (np4 + 1))
        lognorm += -.5 * torch.log(np3)
        lognorm += -.5 * (np4 + 1) * torch.log(.5 * (np1 - ((np2**2) / np3)))
        return torch.sum(lognorm)


class JointNormalGammaPrior(ExpFamilyPrior):
    '''Joint NormalGamma is the distribution over a set of
    :math:`D` dimensional mean vectors :math:`M = (\\mu_1, ...,
    \\mu_K)^T` and the diagonal of a precision matrix :math:`\\lambda`.
    It is defined as:

    .. math::
       p(M, \\lambda | M, \\kappa, a, b) = \\big\\lbrack \\prod_{i=1}^K
            \\mathcal{N} \\big(\\mu_i | m_i, (\\kappa_i \;
            \\text{diag}(\\lambda))^{-1} \\big)  \\big\\rbrack
            \\mathcal{G} \\big( \\lambda | a, b \\big)

    The parameters are defined in the same way as for the
    :any:`beer.NormalGammaPrior`

    Attributes:
        dim (int): Dimension of the mean parameter.
        ncomp (int): Number of Normal densities.
    '''

    def __init__(self, means, scales, shape, rate):
        '''
        Args:
            means (``torch.Tensor``): Mean of the Normal.
            scales (``torch.Tensor``): Scale of the Normal.
            shape (``torch.Tensor``): Shape parameter of the Gamma.
            rate (``torch.Tensor``): Rate parameter of the Gamma.

        '''
        self.ncomp, self.dim = means.size()
        natural_hparams = torch.tensor(torch.cat([
            ((scales * means**2).sum(dim=0) + 2 * rate).view(-1),
            (scales * means).view(-1),
            scales.view(-1),
            2 * shape - 1
        ]), requires_grad=True)
        super().__init__(natural_hparams)

    def split_sufficient_statistics(self, s_stats):
        '''Split the sufficient statistics into 4 groups.

        Args:
            s_stats (``torch.Tensor``): Sufficients statistics to
                split

        Returns:
            ``torch.Tensor``: tuple of sufficient statistics.

        '''
        hnp1 = s_stats[:self.dim]
        hnp2s = s_stats[self.dim: self.dim + self.dim * self.ncomp]
        hnp3s = s_stats[self.dim + self.dim * self.ncomp:
                        self.dim + 2 * self.dim * self.ncomp]
        hnp4 = s_stats[self.dim + 2 * self.dim * self.ncomp:]
        return hnp1, hnp2s.view(self.ncomp, self.dim), \
            hnp3s.view(self.ncomp, self.dim), hnp4

    def log_norm(self, natural_hparams):
        '''Log-normalizing function

        Args:
            natural_hparams (``torch.Tensor``): Natural hyper-parameters
                of the distribution.

        Returns:
            ``torch.Tensor`` of size 1: Log-normalization value.

        '''
        hnp1, hnp2s, hnp3s, hnp4 = self.split_sufficient_statistics(
            natural_hparams)
        lognorm = torch.lgamma(.5 * (hnp4 + 1)).sum()
        lognorm += -.5 * torch.log(hnp3s).sum()
        tmp = ((hnp2s ** 2) / hnp3s).view(self.ncomp, self.dim)
        lognorm += torch.sum(-.5 * (hnp4 + 1) * \
            torch.log(.5 * (hnp1 - tmp.sum(dim=0))))
        return lognorm


class NormalWishartPrior(ExpFamilyPrior):
    '''The Normal-Wishart density defined as:

    .. math::
       p(\\mu, \\Lambda | m, \\kappa, W, \\nu) = \\mathcal{N} \\big(\mu |
        m, (\\kappa \; \\Lambda)^{-1} \\big)
        \\mathcal{W} \\big( \\Lambda | w, \\nu \\big)

    where:

      * :math:`\\mu`, :math:`\\Lambda` are the mean and the precision
        omatrix of a multivariate normal density.
      * :math:`m` is the hyper-parameter mean of the Normal density.
      * :math:`\\kappa` is the hyper-parameter scale of the Normal
        density (scalar).
      * :math:`W` is the hyper-paramater scale matrix of the Wishart
        density.
      * :math:`\\nu` is the hyper-parameter degree of freedom of the
        Wishart density.

    Attributes:
        dim (int): Dimension of the mean parameter.

    '''

    def __init__(self, mean, scale, scale_matrix, dof):
        '''
        Args:
            mean (``torch.Tensor``): Mean of the Normal.
            scale (float): Scale of the normal.
            scale_matrix (``torch.Tensor``): Scale matrix of the
                Wishart.
            dof (float): Degree of freedom of the Wishart.
        '''
        self.dim = mean.size(0)
        inv_scale = torch.inverse(scale_matrix)
        natural_hparams = torch.tensor(torch.cat([
            (scale * torch.ger(mean, mean) + inv_scale).view(-1),
            scale * mean,
            (torch.ones(1) * scale).type(mean.type()),
            (torch.ones(1) * (dof - self.dim)).type(mean.type())
        ]), requires_grad=True)
        super().__init__(natural_hparams)

    def split_sufficient_statistics(self, s_stats):
        '''Split the sufficient statistics into 4 groups.

        Args:
            s_stats (``torch.Tensor``): Sufficients statistics to
                split

        Returns:
            ``torch.Tensor``: tuple of sufficient statistics.

        '''
        grp1, grp2 = s_stats[:self.dim ** 2].view(self.dim, self.dim), \
            s_stats[self.dim ** 2:-2]
        grp3, grp4 = s_stats[-2:]
        return grp1, grp2, grp3, grp4

    def log_norm(self, natural_hparams):
        '''Log-normalizing function

        Args:
            natural_hparams (``torch.Tensor``): Natural hyper-parameters
                of the distribution.

        Returns:
            ``torch.Tensor`` of size 1: Log-normalization value.

        '''
        hnp1, hnp2, hnp3, hnp4 = self.split_sufficient_statistics(
            natural_hparams)
        lognorm = .5 * ((hnp4 + self.dim) * self.dim * math.log(2) - \
            self.dim * torch.log(hnp3))
        lognorm += -.5 * (hnp4 + self.dim) * \
            _logdet(hnp1 - torch.ger(hnp2, hnp2) / hnp3)
        seq = torch.arange(1, self.dim + 1, 1).type(natural_hparams.type())
        lognorm += torch.lgamma(.5 * (hnp4 + self.dim + 1 - seq)).sum()
        return lognorm


class JointNormalWishartPrior(ExpFamilyPrior):
    '''Joint Normal-Wishart is the distribution over a set of
    :math:`D` dimensional mean vectors :math:`M = (\\mu_1, ...,
    \\mu_K)^T` and a precision matrix :math:`\\Lambda`.
    It is defined as:

    .. math::
       p(M, \\lambda | M, \\kappa, W, \\nu) = \\big\\lbrack \\prod_{i=1}^K
            \\mathcal{N} \\big(\\mu_i | m_i, (\\kappa_i \;
            \\Lambda)^{-1} \\big)  \\big\\rbrack
            \\mathcal{W} \\big( \\Lambda | W, \\nu \\big)

    The parameters are defined in the same way as for the
    :any:`beer.NormalWishartPrior`

    Attributes:
        dim (int): Dimension of the mean parameter.
        ncomp (int): Number of Normal densities.
    '''

    def __init__(self, means, scales, scale_matrix, dof):
        '''
        Args:
            means (``torch.Tensor``): Means of the Normal densities.
            scales (float): s of the Normal densities.
            scale_matrix (``torch.Tensor``): Scale matrix of the
                Wishart.
            dof (float): Degree of freedom of the Wishart.
        '''
        self.ncomp, self.dim = means.size()
        inv_scale = torch.inverse(scale_matrix)
        mmT = ((scales.view(-1, 1) * means)[:, None, :] * \
            means[:, :, None]).sum(dim=0)
        natural_hparams = torch.tensor(torch.cat([
            (mmT + inv_scale).view(-1),
            (scales.view(-1, 1) * means).view(-1),
            scales,
            (torch.ones(1) * (dof - self.dim)).type(means.type())
        ]), requires_grad=True)
        super().__init__(natural_hparams)

    def split_sufficient_statistics(self, s_stats):
        '''Split the sufficient statistics into 4 groups.

        Args:
            s_stats (``torch.Tensor``): Sufficients statistics to
                split

        Returns:
            ``torch.Tensor``: tuple of sufficient statistics.

        '''
        grp1 = s_stats[:self.dim ** 2].view(self.dim, self.dim)
        grp2s = s_stats[self.dim ** 2:-(self.ncomp + 1)].view(self.ncomp,
                                                              self.dim)
        grp3s = s_stats[-(self.ncomp + 1):-1]
        grp4 = s_stats[-1]
        return grp1, grp2s, grp3s, grp4

    def log_norm(self, natural_hparams):
        '''Log-normalizing function

        Args:
            natural_hparams (``torch.Tensor``): Natural hyper-parameters
                of the distribution.

        Returns:
            ``torch.Tensor`` of size 1: Log-normalization value.

        '''
        hnp1, hnp2s, hnp3s, hnp4 = self.split_sufficient_statistics(
            natural_hparams)
        lognorm = .5 * ((hnp4 + self.dim) * self.dim * math.log(2) - \
            self.dim * torch.log(hnp3s).sum())
        quad_exp = ((hnp2s[:, None, :] * hnp2s[:, :, None]) / \
            hnp3s[:, None, None]).sum(dim=0)
        lognorm += -.5 * (hnp4 + self.dim) * _logdet(hnp1 - quad_exp)
        seq = torch.arange(1, self.dim + 1, 1).type(natural_hparams.type())
        lognorm += torch.lgamma(.5 * (hnp4 + self.dim + 1 - seq)).sum()
        return lognorm



########################################################################
# Densities log-normalizer functions.
########################################################################

def kl_div(model1, model2):
    '''Kullback-Leibler divergence between two densities of the same
    type.

    '''
    return _bregman_divergence(model2.log_norm, model1.log_norm,
                               model1.expected_sufficient_statistics,
                               model2.natural_params, model1.natural_params)


########################################################################
# Normal Prior (full cov).
########################################################################

def _normal_fc_split_nparams(natural_params):
    # We need to retrieve the 2 natural parameters organized as
    # follows:
    #   [ np1_1, ..., np1_D^2, np2_1, ..., np2_D]
    #
    # The dimension D is found by solving the polynomial:
    #   D^2 + D - len(self.natural_params) = 0
    dim = int(.5 * (-1 + math.sqrt(1 + 4 * len(natural_params))))
    np1, np2 = natural_params[:int(dim ** 2)].view(dim, dim), \
         natural_params[int(dim ** 2):]
    return np1, np2, dim


def _normal_fc_log_norm(natural_params):
    np1, np2, _ = _normal_fc_split_nparams(natural_params)
    inv_np1 = torch.inverse(np1)
    return -.5 * _logdet(-2 * np1) - .25 * ((np2[None, :] @ inv_np1) @ np2)[0]


def NormalFullCovariancePrior(mean, cov):
    '''Create a Normal density prior.

    Args:
        mean (Tensor): Expected mean.
        cov (Tensor): Expected covariance of the mean.

    Returns:
        ``NormalFullCovariancePrior``: A Normal density.

    '''
    prec = torch.inverse(cov)
    natural_params = torch.tensor(torch.cat([
        -.5 * prec.contiguous().view(-1),
        prec @ mean,
    ]), requires_grad=True)
    #return ExpFamilyPrior(natural_params, _normal_fc_log_norm)


########################################################################
# Normal Prior (isotropic cov).
########################################################################

def _normal_iso_split_nparams(natural_params):
    np1, np2 = natural_params[0], natural_params[1:]
    #return np1, np2


def _normal_iso_log_norm(natural_params):
    np1, np2 = _normal_iso_split_nparams(natural_params)
    inv_np1 = 1 / np1
    logdet = len(np2) * torch.log(-2 * np1)
    #return -.5 * logdet - .25 * inv_np1 * (np2[None, :] @ np2)


def NormalIsotropicCovariancePrior(mean, variance):
    '''Create a Normal density prior with isotropic covariance matrix.

    Args:
        mean (Tensor): Expected mean.
        variance (Tensor): The variance parameter.

    Returns:
        ``NormalIsotropicPrior``: A Normal density.

    '''
    prec = 1 / variance
    natural_params = torch.tensor(torch.cat([
        -.5 * prec,
        prec * mean,
    ]), requires_grad=True)
    #return ExpFamilyPrior(natural_params, _normal_iso_log_norm)


########################################################################
# Matrix Normal Prior.
########################################################################

def _matrixnormal_fc_split_nparams(natural_params, dim1, dim2):
    np1, np2 = natural_params[:int(dim1 ** 2)].view(dim1, dim1), \
         natural_params[int(dim1 ** 2):].view(dim1, dim2)
    #return np1, np2


def _matrixnormal_fc_log_norm(natural_params, dim1, dim2):
    np1, np2 = _matrixnormal_fc_split_nparams(natural_params, dim1, dim2)
    inv_np1 = torch.inverse(np1)
    #mat1, mat2 = np2.t() @ inv_np1, np2
    #trace_mat1_mat2 = mat1.view(-1) @ mat2.t().contiguous().view(-1)
    #return -.5 * dim2 * _logdet(-2 * np1) - .25 * torch.trace(np2.t() @ inv_np1 @ np2)


def MatrixNormalPrior(mean, cov):
    '''Create a Matrix Normal density prior.

    Note:
        The ``MatrixNormalPrior`` is a special case of the Matrix
        Normal density with a single scale matrix (the other is
        assumed to be the identity matrix).

    Args:
        mean (Tensor (q x d)): Expected mean.
        cov (Tensor (q x q)): Expected covariance of the mean.

    Returns:
        ``NormalPrior``: A Normal density.

    '''
    prec = torch.inverse(cov)
    natural_params = torch.tensor(torch.cat([
        -.5 * prec.contiguous().view(-1),
        (prec @ mean).view(-1),
    ]), dtype=mean.dtype, requires_grad=True)
    #return ExpFamilyPrior(natural_params, _matrixnormal_fc_log_norm,
    #                      args={'dim1': mean.size(0), 'dim2': mean.size(1)})


########################################################################
# Gamma Prior.
########################################################################


def _gamma_log_norm(natural_params):
    return torch.lgamma(natural_params[0] + 1) - \
        (natural_params[0] + 1) * torch.log(- natural_params[1])


def GammaPrior(shape, rate):
    '''Create a Gamma density prior.

    Args:
        shape (scalar torch Tensor): Expected mean.
        rate (scalar torch Tensor): Expected covariance of the mean.

    Returns:
        ``NormalPrior``: A Normal density.

    '''
    natural_params = torch.tensor(torch.cat([shape - 1, -rate]),
                                  requires_grad=True)
    #return ExpFamilyPrior(natural_params, _gamma_log_norm)
