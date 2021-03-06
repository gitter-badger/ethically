import copy
import os
import warnings

import matplotlib.pylab as plt
import numpy as np
import pandas as pd
import seaborn as sns
from gensim.models.keyedvectors import KeyedVectors
from pkg_resources import resource_filename
from sklearn.decomposition import PCA
from sklearn.svm import LinearSVC
from tqdm import tqdm

from tabulate import tabulate

from ..consts import RANDOM_STATE
from .utils import (
    cosine_similarity, normalize, project_reject_vector, project_vector,
    reject_vector, update_word_vector,
)


DIRECTION_METHODS = ['single', 'sum', 'pca']
DEBIAS_METHODS = ['neutralize', 'hard', 'soft']
FIRST_PC_THRESHOLD = 0.5
MAX_NON_SPECIFIC_EXAMPLES = 1000


class BiasWordsEmbedding:
    """Audit and Adjust a Bias in English Words Embedding.

    :param model: Words embedding model of ``gensim.model.KeyedVectors``
    :param bool only_lower: Whether the words embedding contrains
                            only lower case words
    :param bool verbose: Set vebosity
    """

    def __init__(self, model, only_lower=False, verbose=False):
        if not isinstance(model, KeyedVectors):
            raise TypeError('model should be of type KeyedVectors, not {}'
                            .format(type(model)))

        self.model = model

        # TODO: write unitest for when it is False
        self.only_lower = only_lower

        self._verbose = verbose

        self.direction = None
        self.positive_end = None
        self.negative_end = None

    def __copy__(self):
        bias_words_embedding = self.__class__(self.model)
        bias_words_embedding.direction = copy.deepcopy(self.direction)
        bias_words_embedding.positive_end = copy.deepcopy(self.positive_end)
        bias_words_embedding.negative_end = copy.deepcopy(self.negative_end)
        return bias_words_embedding

    def __deepcopy__(self, memo):
        bias_words_embedding = copy.copy(self)
        bias_words_embedding.model = copy.deepcopy(bias_words_embedding.model)
        return bias_words_embedding

    def __getitem__(self, key):
        return self.model[key]

    def __contains__(self, item):
        return item in self.model

    def _filter_words_by_model(self, words):
        return [word for word in words if word in self]

    def _is_direction_identified(self):
        if self.direction is None:
            raise RuntimeError('The direction was not identified'
                               ' for this {} instance'
                               .format(self.__class__.__name__))

    # There is a mistake in the article
    # it is written (section 5.1):
    # "To identify the gender subspace, we took the ten gender pair difference
    # vectors and computed its principal components (PCs)"
    # however in the source code:
    # https://github.com/tolga-b/debiaswe/blob/10277b23e187ee4bd2b6872b507163ef4198686b/debiaswe/we.py#L235-L245
    def _identify_subspace_by_pca(self, definitional_pairs, n_components):
        matrix = []

        for word1, word2 in definitional_pairs:
            vector1 = normalize(self[word1])
            vector2 = normalize(self[word2])

            center = (vector1 + vector2) / 2

            matrix.append(vector1 - center)
            matrix.append(vector2 - center)

        pca = PCA(n_components=n_components)
        pca.fit(matrix)

        if self._verbose:
            table = enumerate(pca.explained_variance_ratio_, start=1)
            headers = ['Principal Component',
                       'Explained Variance Ratio']
            print(tabulate(table, headers=headers))

        return pca

    # TODO: add the SVD method from section 6 step 1
    # It seems there is a mistake there, I think it is the same as PCA
    # just with repleacing it with SVD
    def _identify_direction(self, positive_end, negative_end,
                            definitional, method='pca'):
        if method not in DIRECTION_METHODS:
            raise ValueError('method should be one of {}, {} was given'.format(
                DIRECTION_METHODS, method))

        if positive_end == negative_end:
            raise ValueError('positive_end and negative_end'
                             'should be different, and not the same "{}"'
                             .format(positive_end))
        if self._verbose:
            print('Identify direction using {} method...'.format(method))

        direction = None

        if method == 'single':
            direction = normalize(normalize(self[definitional[0]])
                                  - normalize(self[definitional[1]]))

        elif method == 'sum':
            groups = list(zip(*definitional))

            group1_sum_vector = np.sum([self[word]
                                        for word in groups[0]], axis=0)
            group2_sum_vector = np.sum([self[word]
                                        for word in groups[1]], axis=0)

            diff_vector = (normalize(group1_sum_vector)
                           - normalize(group2_sum_vector))

            direction = normalize(diff_vector)

        elif method == 'pca':
            pca = self._identify_subspace_by_pca(definitional, 10)
            if pca.explained_variance_ratio_[0] < FIRST_PC_THRESHOLD:
                raise RuntimeError('The Explained variance'
                                   'of the first principal component should be'
                                   'at least {}, but it is {}'
                                   .format(FIRST_PC_THRESHOLD,
                                           pca.explained_variance_ratio_[0]))
            direction = pca.components_[0]

        # if direction is oposite (e.g. we cannot control
        # what the PCA will return)
        ends_diff_projection = cosine_similarity((self[positive_end]
                                                  - self[negative_end]),
                                                 direction)
        if ends_diff_projection < 0:
            direction = -direction  # pylint: disable=invalid-unary-operand-type

        self.direction = direction
        self.positive_end = positive_end
        self.negative_end = negative_end

    def project_on_direction(self, word):
        """Project the normalized vector of the word on the direction.

        :param str word: The word tor project
        :return float: The projection scalar
        """

        self._is_direction_identified()

        vector = self[word]
        projection_score = self.model.cosine_similarities(self.direction,
                                                          [vector])[0]
        return projection_score

    def _calc_projection_scores(self, words):
        self._is_direction_identified()

        df = pd.DataFrame({'word': words})

        # TODO: maybe using cosine_similarities on all the vectors?
        # it might be faster
        df['projection'] = df['word'].apply(self.project_on_direction)
        df = df.sort_values('projection', ascending=False)

        return df

    def plot_projection_scores(self, words,
                               ax=None, axis_projection_step=None):
        """Plot the projection scalar of words on the direction.

        :param list words: The words tor project
        :return: The ax object of the plot
        """

        self._is_direction_identified()

        projections_df = self._calc_projection_scores(words)
        projections_df['projection'] = projections_df['projection'].round(2)

        if ax is None:
            _, ax = plt.subplots(1)

        if axis_projection_step is None:
            axis_projection_step = 0.1

        cmap = plt.get_cmap('RdBu')
        projections_df['color'] = ((projections_df['projection'] + 0.5)
                                   .apply(cmap))

        most_extream_projection = (projections_df['projection']
                                   .abs()
                                   .max()
                                   .round(1))

        sns.barplot(x='projection', y='word', data=projections_df,
                    palette=projections_df['color'])

        plt.xticks(np.arange(-most_extream_projection, most_extream_projection,
                             axis_projection_step))
        plt.title('← {} {} {} →'.format(self.negative_end,
                                        ' ' * 20,
                                        self.positive_end))

        plt.xlabel('Direction Projection')
        plt.ylabel('Words')

        return ax

    def plot_dist_projections_on_direction(self, word_groups, ax=None):
        """Plot the projection scalars distribution on the direction.

        :param dict word_groups word: The groups to projects
        :return float: The ax object of the plot
        """

        if ax is None:
            _, ax = plt.subplots(1)

        names = sorted(word_groups.keys())

        for name in names:
            words = word_groups[name]
            label = '{} (#{})'.format(name, len(words))
            vectors = [self[word] for word in words]
            projections = self.model.cosine_similarities(self.direction,
                                                         vectors)
            sns.distplot(projections, hist=False, label=label, ax=ax)

        plt.axvline(0, color='k', linestyle='--')

        plt.title('← {} {} {} →'.format(self.negative_end,
                                        ' ' * 20,
                                        self.positive_end))
        plt.xlabel('Direction Projection')
        plt.ylabel('Density')
        ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))

        return ax

    def calc_direct_bias(self, neutral_words, c=None):
        """Calculate the direct bias.

        Based on the projection of neuteral words on the direction.

        :param list neutral_words
        :param c: Strictness of bias measuring
        :type c: float or None
        :return: The direct bias
        """

        if c is None:
            c = 1

        projections = self._calc_projection_scores(neutral_words)['projection']
        direct_bias_terms = np.abs(projections) ** c
        direct_bias = direct_bias_terms.sum() / len(neutral_words)

        return direct_bias

    def calc_indirect_bias(self, word1, word2):
        """Calculate the indirect bias between two words.

        Based on the amount of shared projection of the words on the direction.

        Also called PairBias.
        :param str word1: First word
        :param str word2: Second word
        :type c: float or None
        :return The indirect bias between the two words
        """

        self._is_direction_identified()

        vector1 = normalize(self[word1])
        vector2 = normalize(self[word2])

        perpendicular_vector1 = reject_vector(vector1, self.direction)
        perpendicular_vector2 = reject_vector(vector2, self.direction)

        inner_product = vector1 @ vector2
        perpendicular_similarity = cosine_similarity(perpendicular_vector1,
                                                     perpendicular_vector2)

        indirect_bias = ((inner_product - perpendicular_similarity)
                         / inner_product)
        return indirect_bias

    def _extract_neutral_words(self, specific_words):
        extended_specific_words = set()

        # because or specific_full data was trained on partial words embedding
        for word in specific_words:
            extended_specific_words.add(word)
            extended_specific_words.add(word.lower())
            extended_specific_words.add(word.upper())
            extended_specific_words.add(word.title())

        neutral_words = [word for word in self.model.vocab
                         if word not in extended_specific_words]

        return neutral_words

    def _neutralize(self, neutral_words):
        self._is_direction_identified()

        if self._verbose:
            neutral_words_iter = tqdm(neutral_words)
        else:
            neutral_words_iter = iter(neutral_words)

        for word in neutral_words_iter:
            neutralized_vector = reject_vector(self[word],
                                               self.direction)
            update_word_vector(self.model, word, neutralized_vector)

        self.model.init_sims(replace=True)

    def _equalize(self, equality_sets):
        for equality_set_words in equality_sets:
            equality_set_vectors = [normalize(self[word])
                                    for word in equality_set_words]
            center = np.mean(equality_set_vectors, axis=0)
            (projected_center,
             rejected_center) = project_reject_vector(center,
                                                      self.direction)

            for word, vector in zip(equality_set_words, equality_set_vectors):
                projected_vector = project_vector(vector, self.direction)

                projected_part = normalize(projected_vector - projected_center)
                scaling = np.sqrt(1 - np.linalg.norm(rejected_center)**2)

                # TODO - in the code it is different - why?
                # equalized_vector = rejected_center + scaling * self.direction
                # https://github.com/tolga-b/debiaswe/blob/10277b23e187ee4bd2b6872b507163ef4198686b/debiaswe/debias.py#L36-L37
                equalized_vector = rejected_center + scaling * projected_part

                update_word_vector(self.model, word, equalized_vector)

        self.model.init_sims(replace=True)

    def debias(self, method='hard', neutral_words=None, equality_sets=None,
               inplace=True):
        """Debias the words embedding.

        :param str method: The method of debiasing.
        :param list neutral_words: List of neutral words
                                   for the neutralize step
        :param list equality_sets: List of equality sets
                                   for the equalize step
        :param bool inplace: Whether to debias the object inplace
                             or return a new one

        .. warning::

          After calling `debias`,
          all the vectors of the words embedding
          will be normalized to unit length.

        """

        # pylint: disable=W0212
        if inplace:
            bias_words_embedding = self
        else:
            bias_words_embedding = copy.deepcopy(self)

        if method not in DEBIAS_METHODS:
            raise ValueError('method should be one of {}, {} was given'.format(
                DEBIAS_METHODS, method))

        if method in ['hard', 'neutralize']:
            if self._verbose:
                print('Neutralize...')
            bias_words_embedding._neutralize(neutral_words)

        if method == 'hard':
            if self._verbose:
                print('Equalize...')
            bias_words_embedding._equalize(equality_sets)

        if inplace:
            return None
        else:
            return bias_words_embedding

    def evaluate_words_embedding(self):
        """Print a words embedding evaluation based on standard protocols.

        1. Word pairs task
        2. Analogies task
        """

        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=FutureWarning)

            if self._verbose:
                print('Evaluate word pairs...')
            word_pairs_path = resource_filename(__name__,
                                                os.path.join('data',
                                                             'evaluation',
                                                             'wordsim353.tsv'))
            word_paris_result = self.model.evaluate_word_pairs(word_pairs_path)

            if self._verbose:
                print('Evaluate analogies...')
            analogies_path = resource_filename(__name__,
                                               os.path.join('data',
                                                            'evaluation',
                                                            'questions-words.txt'))  # pylint: disable=C0301
            analogies_result = self.model.evaluate_word_analogies(analogies_path)  # pylint: disable=C0301

        if self._verbose:
            print()
        print('From Gensim')
        print()
        print('-' * 30)
        print()
        print('Word Pairs Result - WordSimilarity-353:')
        print('~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~')
        print('Pearson correlation coefficient:', word_paris_result[0])
        print('Spearman rank-order correlation coefficient'
              'between the similarities from the dataset'
              'and the similarities produced by the model itself:',
              word_paris_result[1])
        print('Ratio of pairs with unknown words:', word_paris_result[2])
        print()
        print('-' * 30)
        print()
        print('Analogies Result')
        print('~~~~~~~~~~~~~~~~')
        print('Overall evaluation score:', analogies_result[0])

    def learn_full_specific_words(self, seed_specific_words,
                                  max_non_specific_examples=None, debug=None):
        """Learn specific words given a list of seed specific wordsself.

        Using Linear SVM.

        :param list seed_specific_words: List of seed specific words
        :param int max_non_specific_examples: The number of non-specifc words
                                              to sample for training
        :return: List of learned specific words and the classifier object
        """

        if debug is None:
            debug = False

        if max_non_specific_examples is None:
            max_non_specific_examples = MAX_NON_SPECIFIC_EXAMPLES

        data = []
        non_specific_example_count = 0

        for word in self.model.vocab:
            is_specific = word in seed_specific_words

            if not is_specific:
                non_specific_example_count += 1
                if non_specific_example_count <= max_non_specific_examples:
                    data.append((self[word], is_specific))
            else:
                data.append((self[word], is_specific))

        np.random.seed(RANDOM_STATE)
        np.random.shuffle(data)

        X, y = zip(*data)

        X = np.array(X)
        X /= np.linalg.norm(X, axis=1)[:, None]

        y = np.array(y).astype('int')

        clf = LinearSVC(C=1, class_weight='balanced',
                        random_state=RANDOM_STATE)

        clf.fit(X, y)

        full_specific_words = []
        for word in self.model.vocab:
            vector = [normalize(self[word])]
            if clf.predict(vector):
                full_specific_words.append(word)

        if not debug:
            return full_specific_words, clf

        return full_specific_words, clf, X, y
