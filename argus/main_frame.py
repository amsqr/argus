""" The core answer computation component """

from elastic import get_content_elastic, check_unknowns
from keyword_extract import check_keywords
from answer import Question, Answer
from features import gen_features


def get_answer(question):
    """
    :param question: String with the question.
    :return: Filled Answer object.
    """
    a = Answer(Question(question))

    checked = check_keywords(a.q)
    if not checked:
        a.text = 'Didn\'t understand the question'
        return a

    # print warning on weird keywords
    # (XXX: should we give up on these questions instead?)
    check_unknowns(a)
    if len(a.q.unknown) > 0:
        print 'we have no information on these words:', a.q.unknown

    found_sources, found_anything = get_content_elastic(a, search_all=False)

    if found_sources:
        gen_features(a)
        a.text = answer_all(a)
        return a

    if not found_anything:
        a.text = 'Absolutely no result'
    else:
        a.text = 'No result'
    return a


def answer_all(answer):
    answer.prob = answer.predict()
    print 'FINAL PROBABILITY=', answer.prob
    answer.info = str(answer.prob)
    if answer.prob < 0.5:
        return 'NO'
    return 'YES'
